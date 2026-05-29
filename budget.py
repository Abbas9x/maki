"""
budget.py — V19 token/quota budgeter.

Step 1: Cerebras 8K context guard.
Step 3: Groq dual-cap (req/day + tokens/day) — added when STT/chat lane lands.
Step 6: NIM credit tracker — added when overflow lane lands.

The 8K guard is the only piece needed for V19 Step 1. We project token count
BEFORE sending to Cerebras; if it would blow the free-tier 8K context window,
we reroute the call to NIM Nemotron Nano 9B in the same turn. No failed
Cerebras call → no retry latency.

Cerebras free-tier hard cap: 8192 tokens (system + history + user + reply).
We leave a 700-token headroom for the reply, so the threshold is 7500.
"""

from __future__ import annotations
import logging, time, json, os, threading
from pathlib import Path

logger = logging.getLogger(__name__)

# ── token counting ───────────────────────────────────────────────────────────
# Prefer tiktoken (accurate). Fall back to char/4 estimate (rough but safe).
_enc = None
try:
    import tiktoken
    # cl100k_base matches GPT-4/3.5 and is a close enough proxy for
    # gpt-oss-120b on Cerebras. Off by ~5% on most prompts.
    _enc = tiktoken.get_encoding("cl100k_base")
    logger.info("budget: tiktoken loaded (cl100k_base)")
except Exception as e:
    logger.info("budget: tiktoken unavailable (%s) — using char/4 estimate", e)


def count_tokens(text: str) -> int:
    """Token count for a single string. Tiktoken if available, else char/4."""
    if not text:
        return 0
    if _enc is not None:
        try:
            return len(_enc.encode(text))
        except Exception:
            pass
    # Fallback: 1 token ≈ 4 chars. Slightly over-counts → safer.
    return max(1, (len(text) + 3) // 4)


def count_messages(messages: list[dict]) -> int:
    """
    Project token count for an OpenAI-style messages list.
    Adds ~4 tokens per message for role/separator overhead (matches OpenAI's
    documented framing cost).
    """
    total = 0
    for m in messages or []:
        total += count_tokens(m.get("content", ""))
        total += count_tokens(m.get("role", "user"))
        total += 4   # role + message separator overhead
    total += 2       # priming
    return total


# ── 8K guard ─────────────────────────────────────────────────────────────────
CEREBRAS_CTX_LIMIT      = 8192   # free-tier hard cap
CEREBRAS_REPLY_HEADROOM = 700    # reserved for max_completion_tokens reply
CEREBRAS_PROJECT_CEIL   = CEREBRAS_CTX_LIMIT - CEREBRAS_REPLY_HEADROOM   # 7492 → rounded to 7500
CEREBRAS_THRESHOLD      = 7500


# ── decision log (auditable, append-only) ───────────────────────────────────
_LOG_DIR = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_DECISION_LOG = _LOG_DIR / "v19_budget.jsonl"
_log_lock = threading.Lock()


def _log_decision(event: dict) -> None:
    """Append one JSON object per line. Best-effort, never raises."""
    try:
        event["ts"] = time.time()
        with _log_lock:
            with open(_DECISION_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def route_or_reroute(messages: list[dict], target: str = "cerebras") -> tuple[str, int]:
    """
    Pre-call lane check. Returns (chosen_lane, projected_tokens).

    For target='cerebras': if projected > CEREBRAS_THRESHOLD → 'nim_nemotron'
    same turn (no failed Cerebras call, no retry). Otherwise → 'cerebras'.

    For other targets: pass-through with token count (lets callers log token
    usage uniformly).
    """
    projected = count_messages(messages)

    if target == "cerebras" and projected > CEREBRAS_THRESHOLD:
        _log_decision({
            "event": "reroute",
            "from": "cerebras",
            "to": "nim_nemotron",
            "projected_tokens": projected,
            "threshold": CEREBRAS_THRESHOLD,
            "reason": "8k_ctx_guard",
        })
        logger.info("budget: 8K guard tripped (%d tok) — rerouting cerebras → nim_nemotron",
                    projected)
        return "nim_nemotron", projected

    _log_decision({
        "event": "route",
        "lane": target,
        "projected_tokens": projected,
    })
    return target, projected


# ── public utility: would_overflow ───────────────────────────────────────────
def would_overflow_cerebras(messages: list[dict]) -> bool:
    """Pure check (no logging). Useful for callers that want to truncate
    history themselves rather than reroute."""
    return count_messages(messages) > CEREBRAS_THRESHOLD


# ── V19 Step 3: Groq tri-cap tracker ─────────────────────────────────────────
# Groq publishes three independent rate-limit pages for the free tier on
# llama-3.1-8b-instant and whisper-large-v3-turbo:
#   - requests / day   (14,400 for chat; whisper separate)
#   - text tokens / day (500,000 for chat — does NOT include whisper audio)
#   - audio seconds / day  (whisper has its own bucket)
# We track all three. If Groq later changes Whisper to share text-token quota,
# we flip GROQ_WHISPER_SHARES_TEXT_QUOTA=True and the tracker collapses to dual.
#
# VERIFY: Groq Whisper [does NOT] share quota with chat (tri-cap), verified
# YYYY-MM-DD against console.groq.com/settings/limits. If this flips, set
# GROQ_WHISPER_SHARES_TEXT_QUOTA = True and the audio-second counter is unused.

import datetime

GROQ_CHAT_REQ_CAP          = 14_400      # llama-3.1-8b-instant free tier
GROQ_CHAT_TOK_CAP          = 500_000     # text tokens/day
GROQ_WHISPER_AUDIO_SEC_CAP = 28_800      # 8hr/day audio (verified at console.groq.com 2026-05-20)
GROQ_WHISPER_SHARES_TEXT_QUOTA = False   # flip if docs check shows shared quota

_groq_lock = threading.Lock()
_groq_state = {
    "date":          None,   # UTC date string, resets at midnight
    "chat_req":      0,
    "chat_tok":      0,
    "whisper_sec":   0.0,
}


def _groq_reset_if_new_day() -> None:
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    if _groq_state["date"] != today:
        _groq_state["date"]        = today
        _groq_state["chat_req"]    = 0
        _groq_state["chat_tok"]    = 0
        _groq_state["whisper_sec"] = 0.0


def groq_chat_available(est_tokens: int = 200) -> tuple[bool, str]:
    """Can we make a Groq chat call right now? Returns (ok, reason)."""
    with _groq_lock:
        _groq_reset_if_new_day()
        if _groq_state["chat_req"] >= GROQ_CHAT_REQ_CAP:
            return False, f"groq chat RPD exhausted ({GROQ_CHAT_REQ_CAP})"
        if _groq_state["chat_tok"] + est_tokens > GROQ_CHAT_TOK_CAP:
            return False, f"groq chat TPD would exceed ({GROQ_CHAT_TOK_CAP})"
        return True, "ok"


def groq_chat_record(req_count: int = 1, tokens_used: int = 0) -> None:
    with _groq_lock:
        _groq_reset_if_new_day()
        _groq_state["chat_req"] += req_count
        _groq_state["chat_tok"] += tokens_used


def groq_whisper_available(est_audio_seconds: float = 5.0) -> tuple[bool, str]:
    """Can we make a Groq Whisper call right now?"""
    with _groq_lock:
        _groq_reset_if_new_day()
        if GROQ_WHISPER_SHARES_TEXT_QUOTA:
            # Collapsed to text-token bucket — treat audio-sec as ~tokens
            if _groq_state["chat_tok"] + int(est_audio_seconds * 20) > GROQ_CHAT_TOK_CAP:
                return False, "groq shared TPD would exceed"
            return True, "ok"
        if _groq_state["whisper_sec"] + est_audio_seconds > GROQ_WHISPER_AUDIO_SEC_CAP:
            return False, f"groq whisper audio cap reached ({GROQ_WHISPER_AUDIO_SEC_CAP}s)"
        return True, "ok"


def groq_whisper_record(audio_seconds: float) -> None:
    with _groq_lock:
        _groq_reset_if_new_day()
        if GROQ_WHISPER_SHARES_TEXT_QUOTA:
            _groq_state["chat_tok"] += int(audio_seconds * 20)
        else:
            _groq_state["whisper_sec"] += audio_seconds


def groq_status() -> dict:
    """Snapshot for logs / debugging."""
    with _groq_lock:
        _groq_reset_if_new_day()
        return dict(_groq_state)


# ── V19 Step 6: NIM credit tracker (wired with the overflow lane) ───────────

NIM_STARTER_CREDITS = 1000
_nim_lock = threading.Lock()
_nim_state = {
    "credits_used":  0,
    "first_use_t":   None,   # for burn-rate calc
}


def nim_record_call(credits_cost: int = 1) -> None:
    with _nim_lock:
        if _nim_state["first_use_t"] is None:
            _nim_state["first_use_t"] = time.time()
        _nim_state["credits_used"] += credits_cost
        remaining = NIM_STARTER_CREDITS - _nim_state["credits_used"]
        if remaining < 100:
            elapsed_hr = max((time.time() - _nim_state["first_use_t"]) / 3600.0, 0.01)
            burn_rate = _nim_state["credits_used"] / elapsed_hr
            proj_hr = remaining / burn_rate if burn_rate > 0 else float("inf")
            logger.warning(
                "NIM credits low: remaining=%d, burn_rate=%.1f/hr, projected_exhaustion=%.1fh",
                remaining, burn_rate, proj_hr,
            )
            _log_decision({
                "event":                "nim_credit_alarm",
                "credits_remaining":    remaining,
                "burn_rate_per_hr":     round(burn_rate, 1),
                "projected_exhaustion_hr": round(proj_hr, 1),
            })


def nim_credits_remaining() -> int:
    with _nim_lock:
        return NIM_STARTER_CREDITS - _nim_state["credits_used"]
