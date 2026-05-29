"""
memory.py - Short-term session memory for Maki V2.

Keeps the last N user/Maki turns in RAM only — never persisted to disk.
Used to give Ollama context and to resolve references like "do that again".
"""

import json, logging, os, re, threading, time
from collections import deque

logger = logging.getLogger(__name__)

# RAM window passed to the AI each turn (token budget).
_MAX_TURNS   = 16
# Full-ish history kept on disk for recall ("what did I say earlier?").
_MAX_PERSIST = 400
_HISTORY_FILE = os.path.join("logs", "conversation_history.json")

_history: deque    = deque(maxlen=_MAX_TURNS)   # recent turns → AI context
_persistent: list  = []                          # capped long history → disk
_lock              = threading.Lock()
_session_started   = time.time()
_prev_session_end  = 0.0                          # ts of last turn from a prior run

# Last confirmed action (so "do it again" works)
_last_action: dict = {}
_action_lock       = threading.Lock()


# ── Disk persistence ──────────────────────────────────────────────────────────

def _load_persistent() -> None:
    """Load prior conversation history from disk and seed the RAM window."""
    global _persistent, _prev_session_end
    try:
        if os.path.exists(_HISTORY_FILE):
            with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            _persistent = (data.get("turns") or [])[-_MAX_PERSIST:]
            _prev_session_end = data.get("saved", 0.0)
            # Seed the RAM deque with the tail so Maki has continuity at boot.
            for turn in _persistent[-_MAX_TURNS:]:
                _history.append({"role": turn.get("role", "user"),
                                  "content": turn.get("content", "")})
            logger.info("memory: loaded %d past turns from disk", len(_persistent))
    except Exception as e:
        logger.warning("memory: could not load history: %s", e)
        _persistent = []


def _save_persistent() -> None:
    """Write the capped persistent history to disk (best-effort)."""
    try:
        os.makedirs(os.path.dirname(_HISTORY_FILE) or ".", exist_ok=True)
        tmp = _HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"turns": _persistent[-_MAX_PERSIST:], "saved": time.time()},
                      f, ensure_ascii=False)
        os.replace(tmp, _HISTORY_FILE)
    except Exception as e:
        logger.debug("memory: could not save history: %s", e)


# ── V10 Semantic memory (Ollama nomic-embed-text + numpy cosine) ─────────────
_EMBED_MODEL   = "nomic-embed-text"
_EMBED_URL     = "http://localhost:11434/api/embeddings"
_embed_enabled = True   # auto-disables if Ollama embeddings are unreachable


def _embed(text: str):
    """Return an embedding vector for `text`, or None if unavailable."""
    global _embed_enabled
    if not _embed_enabled or not text or not text.strip():
        return None
    try:
        import requests
        r = requests.post(_EMBED_URL,
                          json={"model": _EMBED_MODEL, "prompt": text[:2000]},
                          timeout=8)
        r.raise_for_status()
        emb = r.json().get("embedding")
        return emb if emb else None
    except Exception as e:
        logger.debug("memory: embed failed (%s) — semantic recall degraded.", e)
        return None


def _cosine(a, b) -> float:
    try:
        import numpy as np
        va = np.asarray(a, dtype=np.float32)
        vb = np.asarray(b, dtype=np.float32)
        denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
        return float(va.dot(vb) / denom) if denom else 0.0
    except Exception:
        return 0.0


def _embed_async(turn: dict) -> None:
    """Compute + attach an embedding to a persistent turn, off the hot path."""
    def _work():
        emb = _embed(turn.get("content", ""))
        if emb:
            with _lock:
                turn["emb"] = emb
    threading.Thread(target=_work, daemon=True, name="mem-embed").start()


def _backfill_embeddings() -> None:
    """After load, embed recent past turns that have no vector yet (background)."""
    def _work():
        time.sleep(8)   # let boot settle before hitting Ollama
        with _lock:
            todo = [t for t in _persistent[-100:]
                    if "emb" not in t and t.get("content")]
        done = 0
        for t in todo:
            emb = _embed(t.get("content", ""))
            if emb:
                with _lock:
                    t["emb"] = emb
                done += 1
        if done:
            with _lock:
                _save_persistent()
            logger.info("memory: backfilled %d semantic embeddings", done)
    threading.Thread(target=_work, daemon=True, name="mem-backfill").start()


# ── History ───────────────────────────────────────────────────────────────────

def add(role: str, text: str) -> None:
    """role: 'user' or 'assistant'. Appends to RAM window AND disk history."""
    if not text:
        return
    with _lock:
        _history.append({"role": role, "content": text})
        turn = {"role": role, "content": text, "ts": time.time()}
        _persistent.append(turn)
        if len(_persistent) > _MAX_PERSIST:
            del _persistent[:-_MAX_PERSIST]
        _save_persistent()
    # V10: compute the semantic embedding off the hot path
    _embed_async(turn)


def get_history() -> list[dict]:
    """Return a copy of the recent RAM window for AI context."""
    with _lock:
        return list(_history)


def get_recent_text(n: int = 4) -> str:
    """Return the last n turns as a readable string for prompts."""
    with _lock:
        turns = list(_history)[-n:]
    lines = []
    for t in turns:
        prefix = "User" if t["role"] == "user" else "Maki"
        lines.append(f"{prefix}: {t['content']}")
    return "\n".join(lines)


def search_history(keyword: str, limit: int = 5) -> list[dict]:
    """
    Find past turns related to `keyword`. V10: hybrid recall —
      • keyword + fuzzy per-word matching (works offline, instant)
      • semantic similarity via embeddings, so "my schoolwork" can match a
        turn that said "calculus project deadline" with no shared words.
    Newest, most-relevant last.
    """
    kw = (keyword or "").lower().strip()
    if not kw:
        return []
    import difflib
    kw_words = [w for w in re.findall(r"[a-z0-9']+", kw) if len(w) > 2]
    q_emb = _embed(keyword)   # None if Ollama embeddings unavailable
    with _lock:
        turns = list(_persistent)
    scored = []
    for idx, t in enumerate(turns):
        content = (t.get("content", "") or "").lower()
        if not content:
            continue
        score = 0.0
        # ── lexical signal ────────────────────────────────────────────────
        if kw in content:
            score += 3.0
        c_words = re.findall(r"[a-z0-9']+", content)
        c_set = set(c_words)
        for w in kw_words:
            if w in c_set:
                score += 1.0
            elif difflib.get_close_matches(w, c_words, n=1, cutoff=0.82):
                score += 0.7
        # ── semantic signal ───────────────────────────────────────────────
        emb = t.get("emb")
        if q_emb and emb:
            sim = _cosine(q_emb, emb)
            if sim > 0.55:                 # only count a meaningful match
                score += sim * 2.5         # weight semantic relevance
        if score > 0:
            scored.append((score + idx * 0.001, t))   # tiny recency nudge
    scored.sort(key=lambda x: x[0])
    return [t for _s, t in scored][-limit:]


def get_last_session_info() -> dict:
    """Info about the previous run for continuity-aware greetings."""
    with _lock:
        return {
            "prev_session_end": _prev_session_end,
            "total_turns":      len(_persistent),
            "last_turn":        (_persistent[-1] if _persistent else None),
        }


def clear() -> None:
    """Clear the RAM window only (disk history is preserved)."""
    with _lock:
        _history.clear()


def clear_all() -> None:
    """Wipe RAM + disk history. Use deliberately."""
    global _persistent
    with _lock:
        _history.clear()
        _persistent = []
        _save_persistent()


# Load prior history at import so Maki boots with continuity.
_load_persistent()
# V10: backfill semantic embeddings for recent past turns (background, post-boot).
_backfill_embeddings()


# ── Last action ───────────────────────────────────────────────────────────────

def set_last_action(decision: dict) -> None:
    with _action_lock:
        _last_action.clear()
        _last_action.update(decision)


def get_last_action() -> dict:
    with _action_lock:
        return dict(_last_action)


# ── V7.5 Screenshot context ───────────────────────────────────────────────────
_last_screenshot_path: str = ""
_last_screenshot_time: float = 0.0
_pending_snip_context: bool = False
_ss_lock = threading.Lock()


def set_last_screenshot(path: str) -> None:
    global _last_screenshot_path, _last_screenshot_time
    import time as _t
    with _ss_lock:
        _last_screenshot_path = path or ""
        _last_screenshot_time = _t.time()


def get_last_screenshot() -> tuple[str, float]:
    with _ss_lock:
        return _last_screenshot_path, _last_screenshot_time


def set_pending_snip(flag: bool) -> None:
    global _pending_snip_context
    with _ss_lock:
        _pending_snip_context = flag


def has_pending_snip() -> bool:
    with _ss_lock:
        return _pending_snip_context


# ── V7.5 Last weather context (for F<->C follow-ups) ─────────────────────────
_last_weather: dict = {}
_w_lock = threading.Lock()


def set_last_weather(temp: float, unit: str, location: str) -> None:
    with _w_lock:
        _last_weather.clear()
        _last_weather.update({"temp": temp, "unit": unit, "location": location})


def get_last_weather() -> dict:
    with _w_lock:
        return dict(_last_weather)


# ── V7.5b Web-search / action-verification context ──────────────────────────
_web_ctx: dict = {
    "pending_web_search_query": "",   # set when Maki offers "want me to search?"
    "last_web_search_query":    "",   # last query actually searched
    "last_action_claimed":      "",   # what Maki said it did
    "last_action_verified":     None, # True/False/None
    "last_action_failed":       "",   # action name that failed, for retry
}
_web_lock = threading.Lock()


def set_pending_web_search(query: str) -> None:
    with _web_lock:
        _web_ctx["pending_web_search_query"] = query or ""


def pop_pending_web_search() -> str:
    with _web_lock:
        q = _web_ctx["pending_web_search_query"]
        _web_ctx["pending_web_search_query"] = ""
        return q


def has_pending_web_search() -> bool:
    with _web_lock:
        return bool(_web_ctx["pending_web_search_query"])


def set_last_web_search(query: str) -> None:
    with _web_lock:
        _web_ctx["last_web_search_query"] = query or ""


def get_last_web_search() -> str:
    with _web_lock:
        return _web_ctx["last_web_search_query"]


def set_action_result(action: str, verified, claimed: str = "") -> None:
    with _web_lock:
        _web_ctx["last_action_claimed"]  = claimed
        _web_ctx["last_action_verified"] = verified
        if verified is False:
            _web_ctx["last_action_failed"] = action


def get_action_context() -> dict:
    with _web_lock:
        return dict(_web_ctx)


# ══════════════════════════════════════════════════════════════════════════
# V18 — Think mode + stop flag (runtime user-controlled state)
# ══════════════════════════════════════════════════════════════════════════

import threading as _th
_v18_lock = _th.Lock()
_think_on  = False    # When True, perception runs BEFORE intent router on every turn
_stop_now  = False    # When True, current TTS + queued transcripts are aborted ASAP


def set_think_mode(enabled: bool) -> None:
    """Toggle deep-reasoning mode (perception runs on every turn instead of as fallback)."""
    global _think_on
    with _v18_lock:
        _think_on = bool(enabled)


def is_think_mode() -> bool:
    with _v18_lock:
        return _think_on


def request_stop() -> None:
    """Signal to halt current TTS + drop pending transcripts."""
    global _stop_now
    with _v18_lock:
        _stop_now = True


def consume_stop() -> bool:
    """Atomically check + clear the stop flag. Returns True once if requested."""
    global _stop_now
    with _v18_lock:
        if _stop_now:
            _stop_now = False
            return True
        return False


def is_stop_pending() -> bool:
    with _v18_lock:
        return _stop_now
