"""
cerebras_planner.py — V20 Tier 2 reasoning layer.

The intelligence-first router. Replaces BGE embedding similarity as the
primary decision-maker for anything that isn't a trivial fast-path command.

Pipeline:
    plan(text, screen_context, recent_history, active_app)
      → builds a structured prompt
      → calls Cerebras gpt-oss-120b
      → parses JSON-shaped action plan
      → returns a Plan dataclass that execute_plan() can dispatch on

Token budget: system prompt + context typically 800-1400 tokens, leaves
6500+ for Cerebras's 8K free-tier limit. budget.would_overflow_cerebras
guards against history bloat.

This module does NOT execute anything. It just decides what to do.
Step 3 (execute_plan) is the dispatcher.
"""

from __future__ import annotations
import json, logging, re, time
from dataclasses import dataclass, asdict
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Action vocabulary (12 verbs Maki can dispatch on) ───────────────────────
VALID_ACTIONS = {
    "CLICK",        # click a UI element by name/description
    "TYPE",         # type text into the focused field
    "KEY",          # press a keyboard shortcut (ctrl+a, enter, etc.)
    "SCROLL",       # scroll up/down/left/right N times
    "OPEN",         # launch an application
    "CLOSE",        # close an application
    "FOCUS",        # bring an app to foreground
    "SCREENSHOT",   # capture screen
    "VISION",       # qwen3-vl:4b — look at screen + answer/describe
    "SEARCH",       # Google/YouTube/web search
    "CHAT",         # conversational reply (no action) — V19 lane router handles
    "MEMORY",       # remember or recall something
}


@dataclass
class Plan:
    intent:     str          # plain-English description of what user wants
    action:     str          # one of VALID_ACTIONS
    target:     str          # what to act on ("chrome", "search bar", etc.)
    params:     dict         # action-specific knobs (e.g. {"keys":"ctrl+a"})
    confidence: float        # 0.0-1.0 — model's self-rated confidence
    reasoning:  str          # why the model picked this action

    def to_dict(self) -> dict:
        return asdict(self)


# ── System prompt (kept under 1800 tokens for 8K-budget safety) ─────────────
_SYSTEM_PROMPT = """You are Maki's action planner. The user spoke to their AI assistant on a Windows PC. Understand their TRUE INTENT — not the literal words.

You output ONE JSON object, nothing else. No prose, no code fences, no explanation outside the JSON.

ACTIONS (pick exactly one):
- CLICK      click a UI element by name/description       target=element name
- TYPE       type text into focused field                  target=text to type
- KEY        press a keyboard shortcut                     target=key combo ("ctrl+a","ctrl+c","enter","alt+left","f5"...)
- SCROLL     scroll the screen                             target=direction ("up","down","left","right"); params={"times":N}
- OPEN       launch an application                         target=app name
- CLOSE      close an application                          target=app name
- FOCUS      bring an app to foreground                    target=app name
- SCREENSHOT capture the screen                            target=""
- VISION     look at screen and answer the user's question target=the question/topic to look for
- SEARCH     web/google/youtube search                     target=search query; params={"engine":"google|youtube|web"}
- CHAT       conversational reply, no system action        target=""
- MEMORY     remember or recall something                  target=fact to store/recall

INTENT REASONING RULES:
- "copy everything", "select all and copy", "grab all of this" → KEY with target="ctrl+a" then a SECOND plan with KEY ctrl+c. Output the FIRST step only; system will ask for next.
- For multi-step combos, prefer the canonical sequence and put the COMBINED keys in target separated by " then ", e.g. target="ctrl+a then ctrl+c".
- "minimize" always means make smaller — never maximize. Use KEY win+down or a CLOSE-like action; prefer KEY "win+down".
- "maximize" / "full screen" → KEY "win+up" or app-specific.
- "thank you", "thanks", "okay", "got it", "cool", "nice", "oh my god" → CHAT (never a tool).
- "think and code X", "I want you to think about X", "write me code for X" → CHAT (the lane router will send to Think mode).
- Pronouns ("that one", "the gameplay one", "it", "the one you mentioned") refer to LAST screen description in conversation history. Use CLICK with target naming that element from history.
- "what's on my screen", "what do you see", "do you see X", "what X is visible", "describe this", "what can I click" → VISION.
- "bye [name]" while inside a chat app → TYPE with target="bye [name]". Don't CLOSE the app.
- "yeah", "yes", "mm-hmm" after a question/action → CHAT (acknowledgment in context).
- Browser nav: "go back" → KEY "alt+left"; "forward" → KEY "alt+right"; "refresh" → KEY "f5"; "new tab" → KEY "ctrl+t"; "close tab" → KEY "ctrl+w".
- If you genuinely cannot tell from the words + context, choose VISION with target=the user's question — looking at the screen first is safer than guessing.

CONFIDENCE:
- 0.95-1.00 = certain
- 0.70-0.94 = probable
- 0.50-0.69 = ambiguous (caller may want to clarify)
- <0.50    = guessing

Respond with EXACTLY this JSON shape:
{"intent":"...","action":"VERB","target":"...","params":{},"confidence":0.0,"reasoning":"..."}"""


# ── Decision log ─────────────────────────────────────────────────────────────
_LOG_DIR = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_DECISION_LOG = _LOG_DIR / "v19_routing.jsonl"


def _log_plan(text: str, plan: Plan | None, raw: str, error: str = "") -> None:
    try:
        entry = {
            "ts":     time.time(),
            "source": "cerebras_planner",
            "text":   (text or "")[:160],
            "plan":   plan.to_dict() if plan else None,
            "raw":    (raw or "")[:300],
            "error":  error,
        }
        with open(_DECISION_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── Context builder ─────────────────────────────────────────────────────────
def build_context(screen_context: str = "",
                  recent_history: list[dict] | None = None,
                  active_app: str = "") -> str:
    """Compact context block appended to the user message."""
    lines = []
    if active_app:
        lines.append(f"Current active app: {active_app}")
    if screen_context:
        lines.append(f"Most recent screen description (may be slightly stale):\n{screen_context[:400]}")
    if recent_history:
        h = "\n".join(f"  {turn.get('role','?').upper()}: {turn.get('content','')[:200]}"
                      for turn in recent_history[-3:])
        if h.strip():
            lines.append(f"Last 3 conversation turns:\n{h}")
    return "\n\n".join(lines) if lines else ""


# ── JSON parsing helpers ────────────────────────────────────────────────────
_JSON_OBJ_RE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.S)


def _extract_json(raw: str) -> dict | None:
    """Find the first {...} object in the raw response and parse it."""
    if not raw: return None
    # Try direct parse first
    try:
        return json.loads(raw.strip())
    except Exception:
        pass
    # Strip code fences if present
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    # Find the first JSON-shaped object
    m = _JSON_OBJ_RE.search(cleaned)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


def _normalize_plan(data: dict, fallback_text: str) -> Plan | None:
    """Coerce a dict into a valid Plan or return None."""
    if not isinstance(data, dict):
        return None
    action = str(data.get("action", "")).upper().strip()
    if action not in VALID_ACTIONS:
        # Try a couple of common synonyms before failing
        synonyms = {"PRESS": "KEY", "HOTKEY": "KEY", "SHORTCUT": "KEY",
                    "LAUNCH": "OPEN", "START": "OPEN", "RUN": "OPEN",
                    "QUIT": "CLOSE", "EXIT": "CLOSE",
                    "LOOK": "VISION", "SEE": "VISION", "OBSERVE": "VISION",
                    "TALK": "CHAT", "RESPOND": "CHAT", "REPLY": "CHAT",
                    "RECALL": "MEMORY", "REMEMBER": "MEMORY",
                    "FIND": "SEARCH", "GOOGLE": "SEARCH", "WEBSEARCH": "SEARCH"}
        action = synonyms.get(action, "")
        if action not in VALID_ACTIONS:
            return None
    try:
        conf = float(data.get("confidence", 0.5))
    except Exception:
        conf = 0.5
    return Plan(
        intent     = str(data.get("intent", fallback_text))[:200],
        action     = action,
        target     = str(data.get("target", ""))[:300],
        params     = data.get("params", {}) if isinstance(data.get("params", {}), dict) else {},
        confidence = max(0.0, min(1.0, conf)),
        reasoning  = str(data.get("reasoning", ""))[:300],
    )


# ── Main entry point ────────────────────────────────────────────────────────
def plan(text: str,
         screen_context: str = "",
         recent_history: list[dict] | None = None,
         active_app: str = "",
         timeout: float = 8.0) -> Plan | None:
    """
    Reason about what the user wants. Returns a Plan, or None on hard failure
    (caller should fall back to legacy routing).
    """
    if not text or not text.strip():
        return None

    try:
        import requests, config
    except Exception as e:
        logger.warning("cerebras_planner: imports failed: %s", e)
        return None

    key = getattr(config, "CEREBRAS_API_KEY", "")
    if not key:
        logger.info("cerebras_planner: no CEREBRAS_API_KEY — caller falls back")
        return None

    user_block = f'User said: "{text}"'
    ctx_block  = build_context(screen_context, recent_history, active_app)
    user_msg   = user_block + ("\n\n" + ctx_block if ctx_block else "")
    messages   = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user",   "content": user_msg},
    ]

    # 8K context guard (shared with the rest of V19)
    try:
        from budget import would_overflow_cerebras
        if would_overflow_cerebras(messages):
            logger.info("cerebras_planner: 8K guard tripped — bailing to legacy router")
            return None
    except Exception:
        pass

    # Breadcrumb instrumentation
    try:
        from breadcrumb import trail
    except Exception:
        from contextlib import nullcontext as trail
        def _t(*a, **kw): return trail()
        trail = _t   # type: ignore

    # ── Primary: Cerebras gpt-oss-120b ──────────────────────────────────────
    raw = ""
    with trail("CEREBRAS_PLANNER", "plan", text=text[:80]):
        try:
            r = requests.post(
                getattr(config, "CEREBRAS_URL",
                        "https://api.cerebras.ai/v1/chat/completions"),
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={
                    "model":               getattr(config, "CEREBRAS_MODEL", "gpt-oss-120b"),
                    "messages":            messages,
                    "max_completion_tokens": 500,
                    "temperature":         0.1,    # planner wants determinism
                    "stream":              False,
                    "response_format":     {"type": "json_object"},
                },
                timeout=timeout,
            )
        except Exception as e:
            logger.info("cerebras_planner: HTTP error: %s", e)
            _log_plan(text, None, "", error=f"http_error: {e}")
            r = None

        if r is not None and r.status_code == 429:
            # V20 Step 4b: 429 → fall through to GROQ SECONDARY PLANNER below.
            # No retry-with-backoff on Cerebras anymore — Groq is fast, free,
            # and uses the same prompt/schema/parser, so it's a real planner
            # not a "delay and hope". Better answer faster.
            logger.info("cerebras_planner: Cerebras 429 — failing over to Groq secondary planner")
            r = None
        elif r is not None and r.status_code != 200:
            logger.info("cerebras_planner: Cerebras HTTP %d: %s", r.status_code, r.text[:200])
            _log_plan(text, None, r.text[:200], error=f"cerebras_http_{r.status_code}")
            r = None
        elif r is not None:
            try:
                data = r.json()
                msg  = data.get("choices", [{}])[0].get("message", {}) or {}
                raw  = (msg.get("content") or msg.get("reasoning") or "").strip()
            except Exception as e:
                _log_plan(text, None, "", error=f"cerebras_parse_envelope: {e}")
                raw = ""

    # ── Secondary planner: Groq llama-3.1-8b-instant ────────────────────────
    # Fires if Cerebras returned 429, a non-200 status, an exception, or an
    # empty body. Same messages, same JSON schema, parsed by the same code
    # below. Groq is ~530ms and free-tier 14,400 RPD — plenty of headroom.
    if not raw:
        try:
            import groq_lane
            if groq_lane.available():
                with trail("GROQ_PLANNER", "plan", text=text[:80]):
                    groq_raw = groq_lane.chat(messages, max_tokens=500, temperature=0.1)
                if groq_raw:
                    raw = groq_raw
                    logger.info("cerebras_planner: Groq secondary planner answered")
        except Exception as e:
            logger.info("cerebras_planner: Groq secondary failed: %s", e)

    if not raw:
        _log_plan(text, None, "", error="both_planners_empty")
        return None

    parsed = _extract_json(raw)
    plan_obj = _normalize_plan(parsed, text) if parsed else None

    if plan_obj is None:
        # Retry once with a stricter format reminder
        logger.info("cerebras_planner: first reply not parseable, retrying once")
        messages_retry = messages + [
            {"role": "assistant", "content": raw[:200]},
            {"role": "user",      "content":
                'Your previous reply was not parseable. Reply with ONLY a single '
                'JSON object on one line: '
                '{"intent":"...","action":"VERB","target":"...","params":{},'
                '"confidence":0.0,"reasoning":"..."}'},
        ]
        try:
            r2 = requests.post(
                getattr(config, "CEREBRAS_URL",
                        "https://api.cerebras.ai/v1/chat/completions"),
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={
                    "model": getattr(config, "CEREBRAS_MODEL", "gpt-oss-120b"),
                    "messages": messages_retry,
                    "max_completion_tokens": 400,
                    "temperature": 0.0,
                    "stream": False,
                    "response_format": {"type": "json_object"},
                },
                timeout=timeout,
            )
            if r2.status_code == 200:
                raw2 = ((r2.json().get("choices", [{}])[0].get("message", {})
                          .get("content") or "").strip())
                parsed = _extract_json(raw2)
                plan_obj = _normalize_plan(parsed, text) if parsed else None
                raw = raw + "\n---RETRY---\n" + raw2
        except Exception as e:
            _log_plan(text, None, raw, error=f"retry_error: {e}")
            return None

    _log_plan(text, plan_obj, raw, error="" if plan_obj else "unparseable_after_retry")
    if plan_obj is None:
        logger.info("cerebras_planner: unable to parse plan from %r", raw[:120])
    return plan_obj
