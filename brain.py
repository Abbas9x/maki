"""
brain.py — Maki V4 decision engine.

Provider priority: Gemini (cloud) → Ollama (local) → Basic (rules only)
Pipeline per utterance:
  1. Clean transcript
  2. Handle pending confirmation / clarification
  3. Safety check
  4. Fast rule-based classifier (zero AI cost)
  5. AI classify: Gemini → Ollama → None
  6. Execute decision (tool / conversation / clarification / confirmation)
  7. Store in memory

Key V4 additions:
  - Gemini 2.5 Flash as primary AI brain (falls back gracefully)
  - Process checking with psutil ("is Chrome running?")
  - Current/live info detection → routes to web search
  - Acknowledgement intent ("okay" → "Got it." — never replays last action)
  - Model identity reporting (always tells real active provider)
  - Processing time tracking
  - Better natural social responses in basic mode
"""

import concurrent.futures as _futures
import json, logging, re, threading, time
import requests

import config, memory, tools, safety
try:
    import weather_tools
except ImportError:
    weather_tools = None
try:
    import world_time_tools
except ImportError:
    world_time_tools = None
try:
    import web_tools
except ImportError:
    web_tools = None
try:
    import window_tools
except ImportError:
    window_tools = None
try:
    import screenshot_tools
except ImportError:
    screenshot_tools = None
try:
    import intents as _intents_mod
    import intent_router as _ir_mod
    _intent_router = _intents_mod.build_router()
    _ir_mod.prewarm(_intent_router)
except Exception as _e:
    _intent_router = None
    import logging as _l
    _l.getLogger(__name__).warning("intent router unavailable: %s", _e)
try:
    import app_index
except ImportError:
    app_index = None
try:
    import agent          # V10 agentic brain — the LLM thinks & calls tools
except Exception as _agent_err:
    agent = None
    logging.getLogger(__name__).warning("agent.py unavailable: %s", _agent_err)

logger = logging.getLogger(__name__)

# ── Provider modes ────────────────────────────────────────────────────────────
MODE_BASIC  = "BASIC"
MODE_OLLAMA = "OLLAMA"
MODE_GEMINI = "GEMINI"

_mode      = MODE_BASIC
_mode_lock = threading.Lock()
_gemini_ok = False             # True once API key validated
_gemini_fail_reason = ""       # Human-readable last failure cause
_gemini_retry_after = 0.0      # monotonic() timestamp; 0 = no cooldown
_ollama_ok = False             # True if Ollama responded with a usable model
_ollama_model_actual = ""      # Actual model name Ollama is using (may differ from config)

# Last processing time in ms (read by main.py for GUI)
_last_ms     = 0
_last_tool   = "none"

# ── Pending states ────────────────────────────────────────────────────────────
_pending              = None
_pending_lock         = threading.Lock()
_pending_confirm      = None
_pending_confirm_lock = threading.Lock()


# ── Mode helpers ──────────────────────────────────────────────────────────────

def get_mode() -> str:
    with _mode_lock:
        return _mode

def _set_mode(m: str):
    global _mode
    with _mode_lock:
        _mode = m

def get_last_process_ms() -> int:
    return _last_ms

def get_last_tool() -> str:
    return _last_tool


_last_cooldown_log = 0.0   # rate-limit the "skipped" log

def _can_use_gemini() -> bool:
    """True if Gemini is configured, key is valid, and not in rate-limit cooldown."""
    global _gemini_retry_after, _last_cooldown_log
    if not _gemini_ok:
        return False
    if _gemini_retry_after > 0:
        now = time.monotonic()
        if now < _gemini_retry_after:
            # Log at most once a minute so the log isn't spammed
            if now - _last_cooldown_log > 60:
                remaining = int(_gemini_retry_after - now)
                logger.info("Gemini skipped: cooldown active (%ds left).", remaining)
                _last_cooldown_log = now
            return False
        _gemini_retry_after = 0.0   # cooldown expired — reset
        logger.info("Gemini rate-limit cooldown expired — re-enabling.")
    return True


def get_active_provider_status() -> str:
    """Return a human-readable explanation of the current AI provider state."""
    mode = get_mode()
    if _can_use_gemini():
        return (f"I'm using Gemini ({config.GEMINI_MODEL}) as my brain right now. "
                f"Fast commands still run through Python directly.")
    if mode == MODE_OLLAMA:
        return (f"I'm using Ollama ({config.OLLAMA_MODEL}) locally. "
                f"Fast commands still use Python directly.")
    # Explain Basic Mode cause
    if not config.GEMINI_API_KEY or config.GEMINI_API_KEY in ("your_key_here", ""):
        reason = "no Gemini API key is set in .env"
    elif not _gemini_ok:
        reason = f"Gemini is disabled — {_gemini_fail_reason or 'auth failure'}"
    elif _gemini_retry_after > 0:
        secs = max(0, int(_gemini_retry_after - time.monotonic()))
        reason = f"Gemini hit its rate limit — cooldown expires in ~{secs}s"
    else:
        reason = "no AI provider responded at startup"
    return (f"I'm in Basic Mode — {reason}. "
            f"Python tools still handle time, app control, search, and other direct commands.")


# ── Pending clarification ─────────────────────────────────────────────────────

def set_pending(p: dict | None):
    global _pending
    with _pending_lock:
        _pending = p

def pop_pending() -> dict | None:
    global _pending
    with _pending_lock:
        p, _pending = _pending, None
    return p

def has_pending() -> bool:
    with _pending_lock:
        return _pending is not None


# ── Pending confirmation ──────────────────────────────────────────────────────

def set_confirm(p: dict | None):
    global _pending_confirm
    with _pending_confirm_lock:
        _pending_confirm = p

def pop_confirm() -> dict | None:
    global _pending_confirm
    with _pending_confirm_lock:
        p, _pending_confirm = _pending_confirm, None
    return p

def has_confirm() -> bool:
    with _pending_confirm_lock:
        return _pending_confirm is not None


# ── Provider management ───────────────────────────────────────────────────────

def check_gemini() -> bool:
    """
    Validate Gemini config — import-only check to avoid burning API quota.
    Per-request calls handle rate limits / auth failures gracefully via _handle_gemini_error.
    Resets cooldown so re-checking always gives a fresh result.
    """
    global _gemini_ok, _gemini_retry_after, _gemini_fail_reason
    _gemini_retry_after = 0.0       # clear cooldown on explicit re-check
    _gemini_fail_reason = ""

    key = config.GEMINI_API_KEY
    if not key or key in ("your_key_here", ""):
        _gemini_ok = False
        _gemini_fail_reason = "no API key configured"
        logger.info("Gemini: no API key — skipping.")
        return False
    try:
        from google import genai  # noqa — import-only; actual calls happen per-request
        _gemini_ok = True
        logger.info("Gemini configured — %s", config.GEMINI_MODEL)
        return True
    except ImportError:
        logger.warning("google-genai not installed — run: pip install google-genai")
        _gemini_ok = False
        _gemini_fail_reason = "google-genai package missing"
        return False
    except Exception as e:
        logger.warning("Gemini check failed: %s", e)
        _gemini_ok = False
        _gemini_fail_reason = str(e)[:80]
        return False


def check_ollama() -> bool:
    """
    Check if Ollama is running. Tries configured model first, then falls back
    to any available model so Ollama doesn't silently fail due to version mismatch.
    Never demotes from GEMINI mode.
    """
    global _ollama_ok, _ollama_model_actual
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=5)
        if r.status_code != 200:
            _ollama_ok = False
            return False
        names = [m.get("name", "") for m in r.json().get("models", [])]
        if not names:
            _ollama_ok = False
            return False

        # V14.2: vision / embedding models must NEVER be picked as the chat
        # model — they break text-only requests and silently corrupt responses.
        # The V14 "slow chat" bug was caused by picking 'qwen3-vl:2b' for
        # 'qwen3:8b' via a too-permissive base-name substring match.
        _BAD_TAGS = ("-vl", "vl:", "embed", "moondream", "minicpm-v",
                     "llava", "vision", "florence")
        def _is_vision_or_embed(n: str) -> bool:
            ln = n.lower()
            return any(b in ln for b in _BAD_TAGS)

        chat_names = [n for n in names if not _is_vision_or_embed(n)]

        # 1. EXACT match on configured tag (e.g. "qwen3:8b")
        cfg_full = config.OLLAMA_MODEL.strip()
        if cfg_full in names:
            _ollama_ok = True
            _ollama_model_actual = cfg_full
            logger.info("Ollama ready — %s", cfg_full)
            return True

        # 2. Base-name match, but ONLY among non-vision chat models
        base = cfg_full.split(":")[0]
        match = next((n for n in chat_names if n.startswith(base + ":") or n == base), None)
        if match:
            _ollama_ok = True
            _ollama_model_actual = match
            logger.info("Ollama ready — %s (base-name match for %s)", match, cfg_full)
            return True

        # 3. Last-resort: any chat model (still skipping vision/embed)
        if chat_names:
            _ollama_model_actual = chat_names[0]
            _ollama_ok = True
            logger.warning("Ollama: configured '%s' not found. Using '%s'.",
                           cfg_full, _ollama_model_actual)
            return True

        # 4. Only vision/embed installed → Ollama unusable as chat backup
        _ollama_ok = False
        logger.warning("Ollama: only vision/embed models installed — no chat model available.")
        return False
    except Exception as e:
        logger.info("Ollama not reachable: %s", e)
        _ollama_ok = False
        return False


def check_providers() -> str:
    """
    Check ALL providers (Gemini + Ollama) regardless of priority — we want to know
    the state of each so we can report both and fall back gracefully per-request.
    Mode is set to the best usable provider, but Ollama availability is always tracked.
    """
    # Always check both so we know what backup is available
    check_gemini()      # sets _gemini_ok / _gemini_retry_after
    check_ollama()      # sets _ollama_ok (never demotes from GEMINI)

    # V10: keep qwen3 resident in VRAM so the agentic brain never cold-starts
    if _ollama_ok and agent is not None:
        try:
            agent.prewarm_ollama()
        except Exception as e:
            logger.debug("Ollama prewarm skip: %s", e)

    if _can_use_gemini():
        _set_mode(MODE_GEMINI)
        logger.info("Active mode: GEMINI (Ollama backup: %s)", _ollama_ok)
        return MODE_GEMINI
    if _ollama_ok:
        _set_mode(MODE_OLLAMA)
        logger.info("Active mode: OLLAMA (Gemini unavailable)")
        return MODE_OLLAMA

    _set_mode(MODE_BASIC)
    logger.warning("Active mode: BASIC — both Gemini and Ollama unavailable.")
    return MODE_BASIC


# ── Speech cleanup ────────────────────────────────────────────────────────────

_WAKE_VARIANTS = re.compile(
    r"^(hey\s+)?(maki|makey|macky|machi|marky|mikey|mickey|monkey|mocky|"
    r"marquee|hockey|mekey|temaki)\s*,?\s*",
    re.I
)
_FILLER = re.compile(
    r"\b(um+|uh+|er+|ah+|like|you know|basically|literally|okay so|so uh|well uh)\b",
    re.I
)
_REPEAT = re.compile(r"\b(\w+)\s+\1\b", re.I)


def clean_transcript(raw: str) -> str:
    text = raw.strip()
    text = _WAKE_VARIANTS.sub("", text).strip()
    text = _FILLER.sub("", text)
    text = _REPEAT.sub(r"\1", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" .,!?")
    return text


def looks_garbled(text: str) -> bool:
    """
    V7.5: only flag clearly bad transcripts (random/noise/single weird word).
    Multi-word entities like 'Barack Obama' or single proper nouns like
    'powershell', 'Claude', 'Discord' are NEVER garbled — they're routed normally
    and either AI or fast-path handles them.
    """
    s = text.strip()
    if not s:
        return True
    words = s.split()
    # 3+ words → never garbled (a real phrase or entity)
    if len(words) >= 3:
        return False
    # 2 words → almost certainly real (proper nouns, "barack obama", "open powershell")
    if len(words) == 2:
        return False
    # 1 word — check it's plausible (mostly letters, not gibberish)
    w = words[0]
    # Pure digits OK (year follow-ups)
    if w.isdigit():
        return False
    # Letters-only words 3+ chars long are accepted (covers powershell, claude, chrome, gmail)
    if re.fullmatch(r"[A-Za-z][A-Za-z'\-]{2,}", w):
        return False
    # Otherwise short / non-alpha / suspect → flag for repeat
    return True


# ── Patterns used by fast classifier ─────────────────────────────────────────

_ACK_RE = re.compile(
    r"^(okay|ok|alright|got it|cool|sure thing|sounds good|perfect|great|"
    r"noted|understood|i see|fair enough|fine|no worries|all good|nice|"
    r"good|awesome|excellent|wonderful|"
    r"that'?s?\s+(good|fine|great|cool|nice|okay|perfect|all)|"
    r"no\s+that.?s?\s+(fine|okay|good|alright)|"
    r"no\s+that\s+is\s+(fine|okay|good|alright)|"
    r"that\s+is\s+(fine|okay|good|alright))\.?$",
    re.I
)

_AFFIRMATIVE = re.compile(
    r"^(yes|yeah|yep|yup|sure|do it|go ahead|confirm|ok|okay|please|"
    r"go for it|do that|absolutely|fine)\.?$",
    re.I
)
_NEGATIVE = re.compile(
    r"^(no|nope|nah|cancel|stop|don't|dont|never mind|nevermind|abort|skip)\.?$",
    re.I
)

# Broader "yes" — catches "yeah sure", "yes please", "go ahead and do it" (V7.5b)
_AFFIRM_BROAD = re.compile(
    r"^\s*(yeah?\s*sure|sure\s*thing|yes\s*please|ok(?:ay)?\s*(?:do\s*it)?|"
    r"go\s+ahead|do\s+it|please\s+do|yeah?|yep|yup|sure|absolutely|"
    r"sounds?\s+good|go\s+for\s+it|please)\b",
    re.I,
)

# Correction / complaint phrases (V7.5b)
_CORRECTION_RE = re.compile(
    r"\byou\s+did\s*n.?t\s+(open|do|launch|start|run)\s+(it|that|anything|any\s*thing)\b|"
    r"\byou\s+did\s*n.?t\s+do\s+(it|that|anything)\b|"
    r"\bnothing\s+(opened|happened|worked|launched)\b|"
    r"\bit\s+did\s*n.?t\s+(work|open|launch|do\s+anything)\b|"
    r"\b(try|do)\s+(it|that)?\s*again\b|"
    r"\bthat.?s\s+wrong\b|"
    r"\byou\s+failed\b",
    re.I,
)

# "answer yourself / don't open browser" → force direct AI/knowledge answer
_ANSWER_YOURSELF_RE = re.compile(
    r"\b(no[, ]+)?answer\s+(it\s+|that\s+)?yourself\b|"
    r"\bdon.?t\s+(open|use)\s+(?:the\s+)?(browser|web|search|google)\b|"
    r"\bjust\s+(tell|answer)\s+me\b|"
    r"\btell\s+me\s+(yourself|directly|the\s+answer)\b|"
    r"\bi.?m\s+asking\s+you\b",
    re.I,
)

# Current / live info patterns — VERY narrow. Only fire when query truly needs
# real-time data. General knowledge questions go straight to Gemini/Ollama.
# Weather/world time have their own dedicated tools (handled earlier).
_CURRENT_INFO_RE = re.compile(
    r"\b("
    r"(?:current|live|real.?time|today.?s?)\s+(?:price|cost|rate|value|exchange\s+rate)\b|"
    r"stock\s+(?:price|market|value|ticker)\b|"
    r"(?:latest|current|today.?s?|breaking)\s+news\b|"
    r"(?:live|current)\s+(?:score|standings?|rankings?|leaderboard)\b|"
    r"who\s+(?:won|is\s+winning)\s+(?:the\s+)?\w+\s+(?:game|match|race)\b|"
    r"what.?s\s+(?:happening|going\s+on)\s+(?:right\s+now|today|currently)\b"
    r")\b",
    re.I,
)


def _is_current_info(text: str) -> bool:
    return bool(_CURRENT_INFO_RE.search(text))


# ── Fast rule-based classifier ────────────────────────────────────────────────

# Filler words that can precede a real command ("okay and what time is it" → "what time is it")
_FILLER_PREFIX_RE = re.compile(
    r"^(okay|ok|alright|right|so|sure|well|and|actually|hey)\s+(and\s+|so\s+)?",
    re.I,
)


def _basic_classify(text: str) -> dict | None:
    """
    Returns a Decision dict if we're certain (no AI needed), or None to let AI handle it.
    Checks in priority order — first match wins.
    """
    t = text.lower().strip()

    # ── Filler-prefix strip: "okay and what time is it" → "what time is it" ──
    # Only do this when it's NOT a bare ack (i.e. there IS content after the filler)
    stripped = _FILLER_PREFIX_RE.sub("", t).strip()
    if stripped and stripped != t:
        sub = _basic_classify(stripped)     # recurse with stripped text
        if sub is not None:
            return sub
        # If no rule matched the stripped text, fall through with original t
        # (so AI can see the full message)

    # ── Social shortcuts FIRST — catches "no that's fine", "how are you" etc. ──
    # (must run before _ACK_RE so nuanced social phrases aren't swallowed as acks)
    for _pat, _resp in _SOCIAL:
        if _pat.search(t):
            return _decision(intent="conversation", spoken_response=_resp, confidence=1.0)

    # ── Acknowledgements — bare "okay / got it / alright" with no pending ─────
    if _ACK_RE.match(t) and not has_confirm():
        return _decision(intent="acknowledgement", confidence=1.0)

    # ── User identity (config-level, works in any mode) ───────────────────────
    if re.search(r"\bwhat.?s?\s*(is\s+)?my\s+name\b|\bwho\s+am\s+i\b", t):
        return _decision(intent="conversation",
                         spoken_response=f"Your name is {config.USER_NAME}.",
                         confidence=1.0)

    # ── Tool-vs-AI explanation ("what model do you use for time?") ───────────
    if re.search(
        r"\bwhat\s+(?:model|ai|engine)\s+(?:do\s+you\s+use|is\s+used|handles?|does?)\s+(?:for\s+)?(?:time|date|clock|disk|local|tools?)\b|"
        r"\bhow\s+(?:do\s+you|does\s+maki)\s+(?:know|get|tell|check)\s+(?:the\s+)?(?:time|date|disk|space)\b|"
        r"\bdo\s+you\s+use\s+(?:gemini|ai|ollama)\s+for\s+(?:time|date|disk|local|tools?)\b",
        t
    ):
        ollama_note = f" Ollama ({_ollama_model_actual or config.OLLAMA_MODEL}) is my backup AI." if _ollama_ok else ""
        return _decision(
            intent="conversation",
            spoken_response=(
                f"For time, date, disk space, and local actions I use Python tools running directly on your PC — "
                f"no AI needed, so it's instant and always accurate. "
                f"Gemini handles reasoning and conversation.{ollama_note}"
            ),
            confidence=1.0,
        )

    # ── Temperature conversion follow-up (V14.4) ───────────────────────────
    # CRITICAL FIX: this MUST NOT eat full weather queries like
    # "weather in tokyo, london and new york in celsius" — those need to fall
    # through to the weather block below.
    # Strategy: require either (a) "convert/change that to X", (b) bare
    # "in/to X" with nothing else, or (c) "X" alone — AND no city pattern
    # ("in <place>") elsewhere in the sentence.
    # "temperature in <city>" is a fresh weather query — UNLESS <city> is
    # actually "celsius" or "fahrenheit" (i.e. user wants a conversion, not
    # a lookup for a city called "celsius").
    _looks_like_fresh_weather_query = bool(re.search(
        r"\b(?:weather|temperature|temp|forecast)\s+(?:like\s+)?in\s+"
        r"(?!celsius\b|fahrenheit\b|c\b|f\b)[a-z]",
        t,
    ))
    _temp_c = None; _temp_f = None
    if not _looks_like_fresh_weather_query:
        # The patterns are anchored (^...$) so they don't bleed into longer
        # sentences with city names.
        _UNIT_C = r"(?:celsius|c|centigrade)"
        _UNIT_F = r"(?:fahrenheit|f)"
        def _convert_match(unit):
            return (
                re.match(rf"^(?:convert|change|put|make)\s+(?:that|it|this|them|those)\s+"
                         rf"(?:in)?to\s+{unit}\b", t)
                or re.match(rf"^(?:in|to)\s+{unit}\s*$", t)
                or re.match(rf"^(?:show|give|tell)\s+me\s+(?:it\s+|that\s+)?"
                            rf"(?:in\s+)?{unit}\s*$", t)
                or re.match(rf"^(?:make|put)\s+(?:it|that)\s+{unit}\s*$", t)
                or re.match(rf"^what.?s\s+(?:that|it)\s+in\s+{unit}\s*\??$", t)
                or re.match(rf"^convert\s+(?:them|those|that|it)\s+(?:in|to|into)\s+"
                            rf"{unit}\s*$", t)
                # "give/show/tell me (the) (temperature|weather|temp) in <unit>"
                or re.match(rf"^(?:show|give|tell)\s+me\s+(?:the\s+)?"
                            rf"(?:temperature|weather|temp)\s+in\s+{unit}\s*$", t)
                # "what is (the) temperature in <unit>" (no city)
                or re.match(rf"^what.?s?\s+(?:is\s+)?(?:the\s+)?"
                            rf"(?:temperature|weather|temp)\s+in\s+{unit}\s*\??$", t)
            )
        _temp_c = _convert_match(_UNIT_C)
        _temp_f = _convert_match(_UNIT_F)
    if _temp_c or _temp_f:
        last = memory.get_last_weather()
        if last:
            return _decision(intent="safe_action", action="convert_temp",
                             target="C" if _temp_c else "F", confidence=1.0)

    # ── Memory recall (V9) — "what did I say earlier about X" ────────────────
    if re.search(
        r"\bwhat\s+did\s+i\s+(say|tell\s+you|mention|ask)\b|"
        r"\bdid\s+i\s+(say|tell\s+you|mention|ask)\b|"
        r"\bdo\s+you\s+remember\s+(when|what|me)\b|"
        r"\b(recall|remember)\s+what\b|"
        r"\bwhat\s+(were|was)\s+we\s+(talking|saying)\b|"
        r"\bwhat\s+did\s+we\s+(talk|discuss)\b",
        t,
    ):
        topic_m = re.search(
            r"\b(?:about|regarding|on|when\s+i\s+said|with)\s+(.+)", t
        )
        topic = topic_m.group(1).strip().rstrip("?.!,") if topic_m else ""
        return _decision(intent="safe_action", action="recall_memory",
                         target=topic, confidence=0.9)

    # ── Why Basic Mode / provider status ──────────────────────────────────────
    if re.search(
        r"\bwhy\s+(are\s+you\s+in\s+)?(basic\s+mode|not\s+using\s+(gemini|ai|ollama))\b|"
        r"\bwhat.?s?\s+wrong\s+with\s+(gemini|ollama|ai|you)\b|"
        r"\bwhy\s+(isn.?t|is\s*n.?t)\s+(gemini|ollama|ai)\s*(work|running|active|on)\b|"
        r"\bwhy\s+(basic|no\s+ai|without\s+ai)\b",
        t
    ):
        return _decision(intent="safe_action", action="get_provider_status", confidence=1.0)

    # ── Meta: model/mode identity (V7.5b: broad — catches all phrasings) ──────
    if re.search(
        r"\b(what|which)\s+models?\s+(?:are|do)\s+you\s+(?:using|use|run|have|running)\b|"
        r"\bmodels?\s+are\s+you\s+using\b|"
        r"\b(what|which)\s+models?\s+(?:is|are)\s+(?:running|active|loaded)\b|"
        r"\bwhat\s+model\s+do\s+you\s+run\b|"
        r"\bare\s+you\s+using\s+(gemini|ollama|qwen|python)\b|"
        r"\b(what|which)\s+(ai|llm|brain|engine)\s+(?:are|is|do)\s+you\b|"
        r"\bwhat\s+are\s+you\s+(using|running)\b|"
        r"\b(what|which)\s+(?:model|mode|brain|engine|ai|llm)\b.{0,15}\b(?:using|running|active)\b|"
        r"\bwhich\s+(?:model|brain|ai)\s+is\s+running\b",
        t
    ):
        return _decision(intent="safe_action", action="get_current_mode_and_model", confidence=1.0)

    # ── Meta: permissions / capabilities ─────────────────────────────────────
    if re.search(
        r"\b(what can you do|your (capabilities|permissions|abilities)|"
        r"what (are|do) you (do|have|capable)|help me|what (permissions|access|rights))\b", t
    ):
        return _decision(intent="safe_action", action="get_permissions", confidence=1.0)

    # ── List running apps (V7.5: catches "background", "how many", "in the background") ──
    # 'count' branch: "how many apps are running" → count + short list
    if re.search(
        r"\bhow\s+many\s+(apps?|programs?|processes?|applications?)\s+"
        r"(?:are|am\s+i|do\s+i\s+have)\s*(?:currently\s+)?(?:running|open|active|in\s+the\s+background)\b",
        t,
    ):
        return _decision(intent="safe_action", action="count_running_apps", confidence=1.0)

    if re.search(
        # "what/which apps [or processes] are/is/am I running/open/in background"
        r"\b(what|which|list)\s+(apps?|programs?|processes?|applications?)"
        r"(?:\s+or\s+(?:apps?|programs?|processes?|applications?))?\s+"
        r"(?:are|is|am\s+i|do\s+i\s+have)\s*(?:currently\s+)?"
        r"(?:running|open|active|using|on|in\s+the\s+background)\b|"
        # "what apps are on / are open in the background"
        r"\b(?:what|which)\s+(?:apps?|programs?|processes?)\s+(?:are\s+)?"
        r"(?:on|open|active|running)(?:\s+in\s+the\s+background)?\b|"
        # "what apps am I running / running in the background"
        r"\bapps?\s+am\s+i\s+(?:running|using|have\s+open)(?:\s+in\s+the\s+background)?\b|"
        # "what programs are open"
        r"\bwhat\s+programs?\s+(?:are|am\s+i)\s+(?:running|open|active|using)\b|"
        # "what's currently running / what is running on my PC"
        r"\bwhat\s+(?:is\s+|are\s+)(?:currently\s+)?running\b|"
        r"\bwhat.?s\s+(?:currently\s+)?running\b|"
        # "list running/open apps"
        r"\blist\s+(?:all\s+)?(?:running|open|active)\s+(?:apps?|programs?|processes?)\b|"
        # background-specific
        r"\b(?:apps?|programs?|processes?)\s+(?:running\s+)?in\s+the\s+background\b",
        t
    ):
        return _decision(intent="safe_action", action="list_running_apps", confidence=1.0)

    # ── Retry last app open ("you didn't open it / it didn't open") ──────────
    if re.search(
        r"\b(you\s+)?(did\s*n.?t|didn.?t|haven.?t|couldn.?t|failed\s+to)\s+"
        r"(open|launch|start)\s+(it|that)\b|"
        r"\bit\s+(didn.?t|did\s*n.?t|hasn.?t|haven.?t)\s+(open|launch|start)\b",
        t
    ):
        last = memory.get_last_action()
        if last and last.get("action") == "open_app" and last.get("target"):
            return _decision(intent="safe_action", action="open_app",
                             target=last["target"], confidence=0.9)

    # ── "Open something relaxing" context shortcut ────────────────────────────
    if re.search(r"\bopen\s+something\s+relaxing\b|\bsomething\s+relaxing\b", t):
        return _decision(intent="safe_action", action="search_youtube",
                         target="relaxing music lofi calm study chill",
                         confidence=1.0)

    # ── "Answer yourself / I'm asking you" after a redirect ──────────────────
    if re.search(
        r"\bi.?m\s+asking\s+you\b|\bjust\s+tell\s+me\b|\byour\s+opinion\b|"
        r"\b(no[,\s]+)?answer\s+(it\s+)?yourself\b|"
        r"\bdon.?t\s+(?:open|use)\s+(?:the\s+)?(?:browser|web|search)\b|"
        r"\btell\s+me\s+directly\b",
        t
    ):
        last = memory.get_last_action()
        if last and last.get("action") in ("search_web", "search_google"):
            # Force the AI to actually answer — don't redirect again
            return _decision(intent="conversation", confidence=0.6)

    # ── Process check: "is X running / open / active" ─────────────────────────
    proc_m = re.search(
        r"(?:is|check\s+(?:if|whether)|are)\s+(.+?)\s+(?:running|open|active|on|started|launched)\??$",
        t, re.I
    )
    if not proc_m:
        proc_m = re.search(
            r"(?:check\s+(?:if\s+)?)(.+?)\s+(?:is\s+)?(?:running|open|active)\b",
            t, re.I
        )
    if proc_m:
        raw_target = proc_m.group(1).strip()
        # Strip leading articles and trailing STT artifacts ("is", "are", "it", "that")
        target = re.sub(r"^(a|an|the)\s+", "", raw_target, flags=re.I).strip()
        target = re.sub(r"\s+(is|are|it|that)\s*$", "", target, flags=re.I).strip()
        if target.lower() in ("google", "google.com"):
            return _decision(
                intent="clarification",
                clarification_question=(
                    "Do you mean Chrome (the browser), or do you want to search Google?"
                ),
                confidence=0.5,
            )
        return _decision(intent="safe_action", action="check_process",
                         target=target, confidence=1.0)

    # ── Current/live info → web search (never AI-guess) ──────────────────────
    if _is_current_info(t):
        return _decision(intent="safe_action", action="search_web",
                         target=t, confidence=0.85)

    # ── Relative time ("time after 2 hours / in 30 minutes") ─────────────────
    _REL_TIME_RE = re.search(
        r"(?:what\s+(?:is\s+)?(?:the\s+)?time|time).{0,30}?"
        r"(?:after|in|plus|from\s+now)\s+(\d+)\s+(hour|hr|minute|min)",
        t
    )
    if _REL_TIME_RE:
        n    = int(_REL_TIME_RE.group(1))
        unit = _REL_TIME_RE.group(2).lower()
        hrs  = n if unit.startswith("h") else 0
        mins = n if unit.startswith("m") else 0
        return _decision(intent="safe_action", action="add_time_offset",
                         target=f"{hrs}h{mins}m", confidence=1.0)

    # ── Multi-timezone countries → ask city ───────────────────────────────────
    _MULTI_TZ_COUNTRIES = {"canada", "united states", "us", "usa", "america",
                           "australia", "russia", "brazil", "mexico"}
    _TZ_CITY_HINTS = {
        "canada":        "Toronto, Vancouver, or Calgary",
        "united states": "New York, Los Angeles, or Chicago",
        "us":            "New York, Los Angeles, or Chicago",
        "usa":           "New York, Los Angeles, or Chicago",
        "america":       "New York, Los Angeles, or Chicago",
        "australia":     "Sydney, Melbourne, or Perth",
        "russia":        "Moscow or Vladivostok",
        "brazil":        "São Paulo or Manaus",
        "mexico":        "Mexico City or Tijuana",
    }

    # ── Weather (V14.3: covers "tell me / give me / can you tell me / what's") ─
    # "what's the weather in Houston" / "temperature in London" / "is it raining in Karachi"
    # "tell me the weather in pakistan" / "can you tell me the temperature in islamabad"
    # "give me the weather in london" / "how's the weather in tokyo"
    w_m = re.search(
        r"(?:what.?s|what\s+is|how.?s|how\s+is|"
        r"(?:can\s+you\s+)?(?:tell|give)\s+me(?:\s+(?:what)?\s+is)?|"
        r"i\s+want\s+(?:to\s+know\s+)?)?\s*"
        r"(?:the\s+)?(?:current\s+)?(?:weather|temperature|temp|forecast)\s+"
        r"(?:like\s+|right\s+now\s+)?(?:in|at|for)\s+(.+?)(?:\?|$|\s+right\s+now|\s+today)",
        t, re.I,
    )
    if not w_m:
        w_m = re.search(
            r"\bis\s+it\s+(?:raining|snowing|hot|cold|sunny|cloudy|windy)\s+in\s+(.+?)(?:\?|$)",
            t, re.I,
        )
    if not w_m:
        # bare "what's the weather" → use a default location placeholder
        if re.search(
            r"\b(?:what.?s|what\s+is|how.?s|how\s+is)\s+(?:the\s+)?weather\b|"
            r"\bweather\s+(?:right\s+now|today|outside)\b",
            t,
        ):
            return _decision(
                intent="clarification",
                action="get_weather",
                clarification_question="Which city should I check the weather for?",
                confidence=0.4,
            )
    if w_m:
        city = w_m.group(1).strip().rstrip(".,!?")
        # V14.4: if the matched "city" is just a unit word, this is actually a
        # convert-temp follow-up — bail so convert_temp can re-handle it.
        if re.fullmatch(r"(?:celsius|fahrenheit|c|f|centigrade)", city, re.I):
            return None
        # V9: strip unit hints ("in celsius", "in fahrenheit", "like") that the
        # regex wrongly swept into the city name — and trailing time fillers.
        city = re.sub(r"\s+(?:like\s+)?in\s+(?:celsius|fahrenheit|c|f)\b.*$",
                      "", city, flags=re.I).strip()
        city = re.sub(r"\s+(like|in\s+celsius|in\s+fahrenheit)\s*$",
                      "", city, flags=re.I).strip()
        city = re.sub(r"\s+(right\s+now|currently|today|please|for\s+me)$",
                      "", city, flags=re.I).strip()
        # V14.3: multi-city weather is now a fast-path too. Split on " and "
        # and "," — get_weather per city in one fast loop, return merged.
        if city and re.search(r"\band\b|,", city):
            cities = re.split(r"\s*(?:,|\band\b)\s*", city)
            cities = [c.strip().rstrip(".,!?") for c in cities if c.strip()]
            if 2 <= len(cities) <= 5:
                return _decision(intent="safe_action", action="get_weather_multi",
                                 target="||".join(cities), confidence=1.0)
        if city:
            return _decision(intent="safe_action", action="get_weather",
                             target=city, confidence=1.0)

    # ── Time ──────────────────────────────────────────────────────────────────
    # Broad match: catches "what time", "what IS the time", "what's the time right now",
    # "time is it", "current time", "tell me the time", "time right now" etc.
    if re.search(
        r"\bwhat\s+(?:is\s+)?(?:the\s+)?time\b|"
        r"\btime\s+is\s+it\b|"
        r"\bcurrent\s+time\b|"
        r"\bwhat.?s\s+the\s+time\b|"
        r"\btell\s+me\s+the\s+time\b|"
        r"\bthe\s+time\s+right\s+now\b|"
        r"\btime\s+right\s+now\b",
        t
    ):
        loc_m = re.search(r"\bin\s+([a-z ,]+?)(?:\?|$| right now| currently)", t)
        if loc_m:
            loc_raw = loc_m.group(1).strip().lower()
            # V14.3: multi-place time IS now a fast-path (was agentic in V10.2)
            if re.search(r"\band\b|,", loc_raw):
                places = re.split(r"\s*(?:,|\band\b)\s*", loc_raw)
                places = [p.strip() for p in places if p.strip()]
                if 2 <= len(places) <= 5:
                    return _decision(intent="safe_action", action="get_time_multi",
                                     target="||".join(places), confidence=1.0)
            if loc_raw in _MULTI_TZ_COUNTRIES:
                cities = _TZ_CITY_HINTS.get(loc_raw, "a specific city")
                return _decision(
                    intent="clarification",
                    clarification_question=(
                        f"{loc_raw.title()} has multiple time zones — "
                        f"which city? For example: {cities}."
                    ),
                )
            return _decision(intent="safe_action", action="get_time_in",
                             target=loc_m.group(1).strip(), confidence=1.0)
        return _decision(intent="safe_action", action="get_current_time", confidence=1.0)

    time_loc = re.search(
        r"(?:time|what time)(?:\s+is\s+it)?\s+(?:in|at|for)\s+(.+?)(?:\?|$| right| now| currently)", t
    )
    if time_loc:
        loc_raw = time_loc.group(1).strip().lower()
        if re.search(r"\band\b", loc_raw):   # multi-place → agentic brain
            return None
        if loc_raw in _MULTI_TZ_COUNTRIES:
            cities = _TZ_CITY_HINTS.get(loc_raw, "a specific city")
            return _decision(
                intent="clarification",
                clarification_question=(
                    f"{loc_raw.title()} has multiple time zones — "
                    f"which city? For example: {cities}."
                ),
            )
        return _decision(intent="safe_action", action="get_time_in",
                         target=time_loc.group(1).strip(), confidence=1.0)

    # ── Date / day ────────────────────────────────────────────────────────────
    _MON = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
            r"aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")

    _DATE_Q = re.search(
        r"what (?:the\s+)?day (?:is(?:\s+it)?|will(?:\s+it)?\s+be|was)\s*"
        r"(?:on\s+|in\s+|for\s+)?(?:the\s+)?"
        r"(\d+(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MON + r"|\b" + _MON + r"\s+\d+)"
        r"(?:\s+\d{4})?(?:\?|$| right| now)",
        t, re.I
    )
    if _DATE_Q:
        raw_date = _DATE_Q.group(1).strip()
        year_m = re.search(r"\b(20\d{2})\b", t)
        if year_m and year_m.group(1) not in raw_date:
            raw_date = f"{raw_date} {year_m.group(1)}"
        return _decision(intent="safe_action", action="calculate_day_of_date",
                         target=raw_date, confidence=1.0)

    _DATE_Q2 = re.search(
        r"what (?:the\s+)?day (?:is(?:\s+it)?|will(?:\s+it)?\s+be|was)\s+"
        r"(?:on\s+|in\s+|for\s+)?(?:the\s+)?(.+?)(?:\?|$)",
        t, re.I
    )
    if _DATE_Q2:
        candidate = _DATE_Q2.group(1).strip()
        if re.search(r"\d|" + _MON, candidate, re.I):
            return _decision(intent="safe_action", action="calculate_day_of_date",
                             target=candidate, confidence=1.0)

    if re.search(
        r"\b(what day is it|what'?s? today|today'?s? date|"
        r"what is today|what is the date|tell me the date|what date is it)\b", t
    ):
        return _decision(intent="safe_action", action="get_current_date", confidence=1.0)

    # ── Largest folders / biggest games (V8) ──────────────────────────────────
    if re.search(
        r"\b(biggest|largest)\s+(folders?|files?|games?|apps?|directories)\b|"
        r"\bwhat.?s\s+(?:taking|using)\s+(?:up\s+)?(?:the\s+)?most\s+(?:space|storage)\b|"
        r"\bbiggest\s+(?:things|stuff)\s+on\s+my\s+(?:pc|computer|drive)\b",
        t,
    ):
        # optional location ("in Downloads", "on D drive")
        loc_m = re.search(r"\bin\s+(?:my\s+)?([\w ]+?)(?:\s+folder)?(?:\?|$)", t)
        base = loc_m.group(1).strip() if loc_m else ""
        return _decision(intent="safe_action", action="get_largest_folders",
                         target=base, confidence=1.0)

    # ── Game / app install size (V8) ──────────────────────────────────────────
    # "how much space is League taking", "how big is Steam", "size of Valorant"
    gs_m = re.search(
        r"\bhow\s+much\s+(?:space|storage|disk)\s+(?:is|does|do)\s+(.+?)\s+"
        r"(?:taking|take|using|use|occupy|occupying|need)\b|"
        r"\bhow\s+big\s+is\s+(?:the\s+)?(.+?)(?:\s+game|\s+install)?(?:\?|$)|"
        r"\bsize\s+of\s+(?:the\s+)?(.+?)(?:\s+game|\s+install)(?:\?|$)",
        t,
    )
    if gs_m:
        gname = next((g for g in gs_m.groups() if g), "").strip().rstrip(".,!?")
        gname = re.sub(r"^(the|my|a)\s+", "", gname, flags=re.I).strip()
        # If it's a known folder alias, fall through to folder-size below.
        _GAME_HINTS = ("league", "valorant", "riot", "steam", "epic", "lol",
                       "rocket league", "discord", "spotify", "game")
        if gname and (any(h in gname for h in _GAME_HINTS)
                      or gname not in ("projectmaki", "maki", "this folder",
                                       "screenshots", "downloads", "documents")):
            if any(h in gname for h in _GAME_HINTS):
                return _decision(intent="safe_action", action="get_game_size",
                                 target=gname, confidence=1.0)

    # ── Folder size (V7.5) — MUST come before disk-space so it doesn't get hijacked
    fs_m = re.search(
        r"\b(?:how\s+much\s+space\s+(?:is\s+)?(?:taken|occupied|used)\s+by|"
        r"size\s+of|how\s+big\s+is)\s+(?:the\s+)?(.+?)(?:\s+folder)?(?:\?|$)",
        t,
    )
    if not fs_m:
        # "<folder> folder size" / "<folder> size"
        fs_m = re.search(
            r"\b(?P<name>[\w\s-]+?)\s+folder\s+size\b|"
            r"\b(?P<name2>projectmaki|maki|screenshots|downloads|documents)\s+size\b",
            t,
        )
    if fs_m:
        target = (fs_m.groupdict().get("name") or fs_m.groupdict().get("name2")
                  or fs_m.group(1)).strip().rstrip(".,!?")
        target = re.sub(r"^(the\s+|this\s+|my\s+)", "", target, flags=re.I).strip()
        # Game-ish target → game size; otherwise folder size
        if target and target.lower() not in ("c", "d", "e", "drive", "ssd", "disk"):
            if any(h in target.lower() for h in
                   ("league", "valorant", "steam", "epic", "riot", "lol")):
                return _decision(intent="safe_action", action="get_game_size",
                                 target=target, confidence=1.0)
            return _decision(intent="safe_action", action="get_folder_size",
                             target=target, confidence=1.0)

    # ── Disk space ────────────────────────────────────────────────────────────
    if re.search(r"\b(free space|disk space|drive space|how full|"
                 r"how much (?:space|storage) (?:is )?(?:left|free|available))\b", t):
        m = re.search(r"\b([c-z])\s*(?:drive|disk|:)\b", t)
        return _decision(intent="safe_action", action="get_disk_space",
                         target=m.group(1).upper() if m else "C", confidence=1.0)

    # ── Sleep PC ──────────────────────────────────────────────────────────────
    if (re.search(r"\b(sleep|hibernate)\b.{0,20}\b(pc|computer|laptop|system)\b", t) or
            re.search(r"\b(pc|computer|laptop)\b.{0,20}\b(sleep|hibernate)\b", t)):
        return _decision(intent="risky_action", action="sleep_pc",
                         confidence=1.0, requires_confirmation=True)

    # ── Camera "take a photo" — can't automate, explain it ───────────────────
    if re.search(r"\b(take|capture|snap)\s+a?\s*(photo|picture|selfie)\b", t):
        return _decision(
            intent="conversation",
            spoken_response=(
                "I can open the Camera app for you, but taking a photo automatically "
                "isn't something I can do yet. Want me to open it?"
            ),
            confidence=1.0,
        )

    # ── Web search ────────────────────────────────────────────────────────────
    web_s = re.search(
        r"(?:search\s+the\s+web|look\s+up|find\s+online|search\s+online|web\s+search|"
        r"find\s+current\s+info(?:\s+about)?|what\s+are\s+the\s+latest)(?:\s+for)?\s+(.+)",
        t, re.I
    )
    if web_s:
        return _decision(intent="safe_action", action="search_web",
                         target=web_s.group(1).strip(), confidence=1.0)

    # ── YouTube search ────────────────────────────────────────────────────────
    yt_and = re.search(r"open\s+youtube\s+and\s+search(?:\s+for)?\s+(.+)", t)
    if yt_and:
        return _decision(intent="safe_action", action="search_youtube",
                         target=yt_and.group(1).strip(), confidence=1.0)

    yt1 = re.search(r"(?:play|watch|search|find|look\s*up)\s+(.+?)\s+on\s+youtube\b", t)
    if yt1:
        return _decision(intent="safe_action", action="search_youtube",
                         target=yt1.group(1).strip(), confidence=1.0)

    yt2 = re.search(r"(?:show)\s+(.+?)\s+on\s+youtube\b", t)
    if yt2 and yt2.group(1).lower() not in ("youtube", "it", "that"):
        return _decision(intent="safe_action", action="search_youtube",
                         target=yt2.group(1).strip(), confidence=1.0)

    yt3 = re.search(r"(?:search\s+youtube|youtube\s+search)(?:\s+for)?\s+(.+)", t)
    if yt3:
        return _decision(intent="safe_action", action="search_youtube",
                         target=yt3.group(1).strip(), confidence=1.0)

    # ── Google search (anchored — won't match "is google running") ───────────
    gs = re.search(
        r"^(?:(?:can\s+you|please|hey|just)\s+)?"
        r"(?:search(?:\s+(?:google|the\s+web))?|google\s+(?:search|for))(?:\s+for)?\s+(.+)",
        t, re.I
    )
    if gs and "youtube" not in t:
        gq = gs.group(1).strip()
        # V9: strip trailing "on google / on the web / for me / please"
        gq = re.sub(r"\s+(on\s+(?:google|the\s+web|the\s+internet)|"
                    r"for\s+me|please|right\s+now)\s*$", "", gq, flags=re.I).strip()
        gq = gq.rstrip("?.!,")
        if gq:
            return _decision(intent="safe_action", action="search_google",
                             target=gq, confidence=1.0)

    # ── PowerShell (and misheard forms like "rocklin powershell") — V7.5b/V8 ─
    if re.search(r"\bpower\s*shell\b", t):
        # Let close/minimize/maximize fall through to their own sections
        if not re.search(
            r"\b(close|quit|kill|exit|terminate|minimize|minimise|"
            r"maximize|maximise|restore|focus)\b", t
        ):
            # Admin / elevated → always confirm (V8)
            if re.search(r"\b(admin|administrator|elevated|as\s+admin)\b", t):
                return _decision(intent="risky_action", action="open_powershell_admin",
                                 target="powershell", requires_confirmation=True,
                                 confidence=0.95)
            # Bare "powershell" / "windows powershell" → confirm before opening
            if re.fullmatch(r"(windows\s+)?power\s*shell", t):
                return _decision(intent="risky_action", action="open_app",
                                 target="powershell", requires_confirmation=True,
                                 confidence=0.9)
            # Any other phrasing containing powershell (incl. misheard) → open it
            return _decision(intent="safe_action", action="open_app",
                             target="powershell", confidence=0.95)

    # ── Screenshots / snipping (V7.5) ─────────────────────────────────────────
    # "take a screenshot and copy it" → take + clipboard
    if re.search(
        r"\b(take|grab|capture)\s+(?:a\s+|the\s+)?screen\s*shot\b.{0,30}?\b(copy|clipboard)\b|"
        r"\bscreenshot\s+(?:and\s+)?copy\b|"
        r"\bcopy\s+(?:a\s+|the\s+)?screen\s*shot\s+to\s+clipboard\b",
        t,
    ):
        return _decision(intent="safe_action", action="take_screenshot_clipboard",
                         confidence=1.0)

    # "take a screenshot", "screenshot this", "capture my screen"
    if re.search(
        r"\b(take|grab|capture)\s+(?:a\s+|the\s+|my\s+)?screen\s*shot\b|"
        r"\bscreenshot\s+(this|the\s+screen|my\s+screen)\b|"
        r"\bcapture\s+my\s+screen\b|"
        r"^screenshot$",
        t,
    ):
        return _decision(intent="safe_action", action="take_screenshot",
                         confidence=1.0)

    # "open the screenshot folder" / "where did you save the screenshot" — FIRST
    # (must beat the save-snip regex which would otherwise grab "where ... save the screenshot")
    if re.search(
        r"\bopen\s+(?:the\s+)?screenshot\s+folder\b|"
        r"\bwhere\s+(?:did\s+you|are\s+the|is\s+the|do\s+you|are\s+my|are)\s+(?:save|saved|stor|put|kept)\w*\s*(?:the\s+|my\s+)?screenshots?\b|"
        r"\bwhere\s+(?:are\s+|is\s+)?(?:the\s+|my\s+)?screenshots?\s+(?:saved|stored|kept)\b|"
        r"\bwhere\s+are\s+my\s+screenshots\b|"
        r"\bshow\s+(?:me\s+)?(?:the\s+)?screenshot\s+folder\b",
        t,
    ):
        return _decision(intent="safe_action", action="open_screenshot_folder",
                         confidence=1.0)

    # "save this snip" / "save clipboard image" — BEFORE snip-area so it isn't swallowed
    if re.search(
        r"\bsave\s+(?:this\s+|that\s+|the\s+)?(snip|clipboard\s+image)\b|"
        r"\bsave\s+(?:this|that)\s+(?:to|as)?\b",
        t,
    ):
        return _decision(intent="safe_action", action="save_clipboard_image",
                         confidence=1.0)

    # "open snipping tool", "open snip"
    if re.search(
        r"\bopen\s+(?:the\s+)?snipping(\s+tool)?\b|"
        r"\bsnipping\s+tool\b|"
        r"\bopen\s+snip\b",
        t,
    ):
        return _decision(intent="safe_action", action="open_snipping_tool",
                         confidence=1.0)

    # "snip this area" — require an object (this/that/area/region) to avoid bare "snip"
    if re.search(
        r"\bsnip\s+(this|that|an?\s+area|a\s+region|here|the\s+screen)\b|"
        r"\blet\s+me\s+snip\b",
        t,
    ):
        return _decision(intent="safe_action", action="snip_area_manual",
                         confidence=1.0)

    # "copy it", "copy the screenshot", "copy the last screenshot"
    if re.search(
        r"\bcopy\s+(it|that|the\s+screenshot|the\s+last\s+screenshot|the\s+screen\s*shot)\b",
        t,
    ):
        # "copy it" needs recent screenshot context
        last = memory.get_last_action()
        if (re.search(r"\bcopy\s+(it|that)\b", t)
                and not (last and last.get("action") in (
                    "take_screenshot", "snip_area_manual", "save_clipboard_image"))):
            # ambiguous "copy it" with no screenshot context → ask
            return _decision(
                intent="clarification", action="copy_last_screenshot",
                clarification_question="What do you want me to copy?",
                confidence=0.4,
            )
        return _decision(intent="safe_action", action="copy_last_screenshot",
                         confidence=1.0)

    # ── Window control (minimize / maximize / restore / focus) ───────────────
    # V10.2: "bring/move/send/put X to (the) front" → focus that window.
    front_m = re.search(
        r"\b(?:bring|move|send|put)\s+(.+?)\s+to\s+(?:the\s+)?front\b", t, re.I,
    )
    if front_m:
        tgt = front_m.group(1).strip().rstrip(".,!?")
        tgt = re.sub(r"^(a|an|the)\s+", "", tgt, flags=re.I).strip()
        tgt = re.sub(r"\s+(browser|tab|window|app)$", "", tgt, flags=re.I).strip()
        if tgt:
            return _decision(intent="safe_action", action="focus_window",
                             target=tgt, confidence=1.0)

    # V9: "take me to X" / "go to X" / "pull up X" also route here (focus).
    win_m = re.search(
        r"\b(minimize|minimise|maximize|maximise|restore|focus|bring\s+up|"
        r"switch\s+to|take\s+me\s+to|pull\s+up|jump\s+to)\s+(?:the\s+)?(.+?)(?:\s+window)?$",
        t, re.I,
    )
    if win_m:
        verb = win_m.group(1).lower()
        target = win_m.group(2).strip().rstrip(".,!?")
        # Strip articles like "the gmail browser" → "gmail"
        target = re.sub(r"^(a|an|the)\s+", "", target, flags=re.I).strip()
        target = re.sub(r"\s+(browser|tab|window|app)$", "", target, flags=re.I).strip()
        if verb in ("minimize", "minimise"):
            action = "minimize_window"
        elif verb in ("maximize", "maximise"):
            action = "maximize_window"
        elif verb == "restore":
            action = "restore_window"
        else:  # focus / bring up / switch to / take me to / pull up / jump to
            action = "focus_window"
        if target:
            return _decision(intent="safe_action", action=action,
                             target=target, confidence=1.0)

    # ── Open app ──────────────────────────────────────────────────────────────
    _APPS = [
        ("riot games","riot client"), ("riot client","riot client"), ("riot","riot client"),
        ("league of legends","league of legends"), ("league","league of legends"),
        ("lol","league of legends"), ("valorant","valorant"),
        ("rocket league","rocket league"), ("rocketleague","rocket league"),
        ("steam","steam"), ("epic games","epic games"), ("epic","epic games"),
        ("discord","discord"), ("spotify","spotify"),
        ("chrome","chrome"), ("google chrome","chrome"), ("browser","chrome"),
        ("edge","edge"), ("firefox","firefox"),
        ("vs code","vs code"), ("vscode","vs code"), ("code","vs code"),
        ("visual studio code","vs code"), ("visual studio","vs code"),
        ("word","word"), ("excel","excel"), ("powerpoint","powerpoint"),
        ("notepad","notepad"),
        ("windows powershell","powershell"), ("power shell","powershell"),
        ("powershell","powershell"),
        ("command prompt","cmd"), ("cmd","cmd"),
        ("terminal","windows terminal"), ("windows terminal","windows terminal"),
        ("docker","docker"), ("docker desktop","docker"),
        ("calculator","calculator"), ("calc","calculator"),
        ("task manager","task manager"), ("taskmgr","task manager"),
        ("photos","photos"), ("camera","camera"), ("paint","paint"),
        ("snipping tool","snipping"), ("snip","snipping"),
        ("nvidia","nvidia"), ("geforce","geforce experience"),
        ("geforce experience","geforce experience"),
    ]
    if re.search(r"\b(open|launch|start|run)\b", t):
        for kw, target in _APPS:
            if kw in t:
                return _decision(intent="safe_action", action="open_app",
                                 target=target, confidence=1.0)
        if re.search(r"\bopen\b", t):
            remainder = re.sub(r"\b(open|launch|start|run)\b", "", t).strip()
            remainder = re.sub(r"^(the|a|an|my|up)\s+", "", remainder).strip()
            remainder = re.sub(r"\s+(please|now|for me)$", "", remainder).strip()
            # Single known site name
            _KNOWN_SITES = {
                "google", "youtube", "gmail", "github", "reddit",
                "twitter", "chatgpt", "claude", "n8n",
            }
            if remainder in _KNOWN_SITES:
                return _decision(intent="safe_action", action="open_named_site",
                                 target=remainder, confidence=1.0)
            # ── V8 fuzzy app resolution — no hardcoded match needed ──────────
            if remainder and remainder not in ("it", "that", "the thing", ""):
                fr = _fuzzy_app(remainder)
                if fr:
                    return fr
            if len(remainder.split()) <= 1 or remainder in ("it", "that", "the thing", ""):
                return _decision(
                    intent="clarification",
                    action="open_app",
                    clarification_question="Which app or website would you like me to open?",
                    confidence=0.3,
                )

    # ── Named websites ────────────────────────────────────────────────────────
    if re.search(r"\b(open|go\s+to|show|visit)\b", t) and "youtube" in t:
        search_part = re.search(r"(?:and\s+)?search(?:\s+for)?\s+(.+)", t)
        if search_part:
            return _decision(intent="safe_action", action="search_youtube",
                             target=search_part.group(1).strip(), confidence=1.0)
        return _decision(intent="safe_action", action="open_named_site",
                         target="youtube", confidence=1.0)

    if re.search(r"\b(open|go\s+to|show|visit)\b", t):
        _SITES = [
            ("gmail","gmail"), ("google","google"), ("github","github"),
            ("reddit","reddit"), ("twitter","twitter"), ("chatgpt","chatgpt"),
            ("claude","claude"), ("n8n","n8n"),
        ]
        for kw, target in _SITES:
            if kw in t:
                return _decision(intent="safe_action", action="open_named_site",
                                 target=target, confidence=1.0)

    # ── Close app ─────────────────────────────────────────────────────────────
    if re.search(r"\b(close|quit|kill|exit|terminate|force\s*quit)\b", t):
        _CLOSE = [
            "riot games", "riot client", "riot",
            "discord", "spotify", "chrome", "google chrome",
            "league of legends", "league", "lol", "valorant", "rocket league",
            "steam", "docker", "vs code", "vscode", "code", "notepad",
            "calculator", "calc", "epic games", "epic", "photos", "edge",
            "firefox", "word", "excel", "powerpoint", "task manager", "nvidia",
            "powershell", "windows powershell", "cmd", "command prompt",
            "windows terminal", "terminal", "camera", "paint", "snipping tool",
            "calendar", "photos app", "settings",
        ]
        for app in _CLOSE:
            if app in t:
                canon = config.APP_ALIASES.get(app, app)
                return _decision(intent="safe_action", action="close_app",
                                 target=canon, confidence=0.95)
        # ── Browser-tab style close: "close gmail", "close the gmail window"
        # Route to window_tools.minimize/focus — we can't close a single tab
        # but we can close the matching window.
        tab_m = re.search(
            r"\bclose\s+(?:the\s+)?(gmail|youtube|github|reddit|twitter|chatgpt|claude|"
            r"notion|linkedin|x\.com)(?:\s+(?:window|tab|browser))?\b",
            t,
        )
        if tab_m:
            return _decision(intent="safe_action", action="close_browser_window",
                             target=tab_m.group(1), confidence=0.85)
        # ── V8 fuzzy close — resolve misheard / non-hardcoded names ──────────
        rem = re.sub(
            r"\b(close|quit|kill|exit|terminate|force\s*quit|the|a|an|my|app|"
            r"window|please|now)\b", "", t).strip()
        if rem and app_index:
            try:
                r = app_index.resolve(rem)
            except Exception:
                r = {}
            if r.get("match") and r.get("confidence", 0) >= 0.90:
                return _decision(intent="safe_action", action="close_app",
                                 target=r["match"], confidence=0.9)
            if r.get("match") and r.get("confidence", 0) >= 0.60:
                cands = r.get("candidates", [r["match"]])
                if len(cands) >= 2:
                    q = f"Did you mean {cands[0].title()} or {cands[1].title()}?"
                else:
                    q = f"Do you want me to close {cands[0].title()}?"
                return _decision(intent="clarification", action="close_app",
                                 target=cands[0], clarification_question=q,
                                 confidence=0.55)
        return _decision(
            intent="clarification",
            action="close_app",
            clarification_question="Which app would you like me to close?",
            confidence=0.3,
        )

    # ── Folders ───────────────────────────────────────────────────────────────
    if re.search(r"\b(open|show|go\s+to)\b.{0,20}\bdownload", t):
        return _decision(intent="safe_action", action="open_folder",
                         target="downloads", confidence=1.0)
    if re.search(r"\b(open|show|go\s+to)\b.{0,20}\bdocument", t):
        return _decision(intent="safe_action", action="open_folder",
                         target="documents", confidence=1.0)
    if re.search(r"\b(open|show|go\s+to)\b.{0,25}\b(maki|project\s+maki)\b", t):
        return _decision(intent="safe_action", action="open_folder",
                         target="maki", confidence=1.0)
    if re.search(r"\b(open|show|go\s+to)\b.{0,20}\bn8n\s*(?:folder|project|dir)?\b", t):
        return _decision(intent="safe_action", action="open_folder",
                         target="n8n", confidence=1.0)

    # ── Spotify shortcut (just "spotify" in sentence without search) ──────────
    if "spotify" in t and "search" not in t:
        return _decision(intent="safe_action", action="open_app",
                         target="spotify", confidence=0.9)

    # ── "Best AI model" — prewritten cautious answer (V7.5b, no AI/browser) ───
    if re.search(r"\b(best|top|greatest)\s+(ai|llm|language)\s+model\b", t):
        return _decision(
            intent="conversation",
            spoken_response=(
                "There isn't one single best model. For coding and reasoning, "
                "Gemini, Claude, and GPT-style models are usually top choices. "
                "For local privacy, Qwen and Llama models are useful but weaker. "
                "Want me to look up the latest rankings?"
            ),
            confidence=1.0,
        )

    # ── General knowledge — question patterns (V7.5b / V9 expanded) ──────────
    # "tell me about X" / "who is X" / "what is X" / "how many X" / "where is X"...
    # By now all tool patterns ran, so "what is the time" etc. are already handled.
    # V9: added how/where/when/why forms so questions hit the fast knowledge
    # path (Gemini→live_lookup) instead of falling through to slow Ollama.
    km = re.search(
        r"^(?:tell\s+me\s+(?:about|more\s+about)|"
        r"give\s+me\s+(?:some\s+)?info(?:rmation)?\s+(?:about|on)|"
        r"info(?:rmation)?\s+(?:about|on)|"
        r"who\s+(?:is|was|are|were)|"
        r"what\s+(?:is|are|was|were|does|do|did)|what.?s|"
        r"how\s+(?:many|much|do|does|did|is|are|long|tall|big|old|far|come)|"
        r"where\s+(?:is|are|was|were|do|does|did|can)|"
        r"when\s+(?:is|are|was|were|did|do|does|will)|"
        r"why\s+(?:is|are|was|were|do|does|did)|"
        r"explain|describe)\s+(.+)",
        t,
    )
    if km:
        topic = km.group(1).strip().rstrip("?.!,")
        topic = re.sub(r"^(a|an|the)\s+", "", topic).strip()
        if topic and len(topic) > 1:
            # V10: knowledge questions go to the AGENTIC BRAIN — it thinks,
            # decides whether to web_search, and answers conversationally.
            return None

    # ── Bare entity / proper noun (e.g. "barack obama", "afghan people") ─────
    # Last resort before AI classify. Conservative: 1-4 word noun-ish phrase,
    # no command verbs, no pronouns/be-verbs (those = conversational filler like
    # "oh there it is", not a thing to look up).
    words = t.split()
    _BARE_STOP = {"yeah", "yes", "yep", "sure", "no", "nope", "nah", "maybe",
                  "thanks", "thank", "please", "hello", "hi", "hey", "bye",
                  "okay", "ok", "alright", "cool", "nice", "great", "wow",
                  "hmm", "uh", "um", "what", "why", "how", "when", "where",
                  "oh", "ah", "huh", "lol", "haha", "wait", "really", "damn",
                  "yay", "ugh", "hm", "mhm", "yep", "nope", "right", "true"}
    if (1 <= len(words) <= 4
            and re.fullmatch(r"[a-z][a-z\s'\-\.]+", t)
            and not (set(words) & _BARE_STOP)
            # exclude conversational fragments — pronouns / be-verbs / "there"/"here"
            and not re.search(
                r"\b(it|that|this|these|those|there|here|is|are|was|were|"
                r"am|be|been|i|you|we|they|he|she)\b", t)
            and not re.search(
                r"\b(open|close|search|play|run|launch|start|stop|minimize|"
                r"maximize|restore|focus|take|copy|snip|show|go|set|turn|put|"
                r"check|tell|give|make|do|get)\b", t)):
        # V10: bare entities ("barack obama") → agentic brain decides & answers
        return None

    return None  # → agentic brain


# ── Fuzzy app resolver (V8) ──────────────────────────────────────────────────

def _fuzzy_app(name: str) -> dict | None:
    """
    Resolve a possibly-misheard app name via app_index.
      confidence >= 0.90 → open it directly
      0.60-0.89          → clarification ("Did you mean X or Y?")
      < 0.60             → None (caller handles)
    """
    if not app_index or not name:
        return None
    try:
        r = app_index.resolve(name)
    except Exception as e:
        logger.debug("app_index.resolve failed: %s", e)
        return None
    match = r.get("match")
    conf  = r.get("confidence", 0.0)
    cands = r.get("candidates", [])
    if not match:
        return None
    if conf >= 0.90:
        logger.info("Fuzzy app: %r → %r (conf=%.2f)", name, match, conf)
        return _decision(intent="safe_action", action="open_app",
                         target=match, confidence=0.95)
    if conf >= 0.60:
        # Medium confidence — confirm before acting
        uniq = []
        for c in cands:
            if c not in uniq:
                uniq.append(c)
        if len(uniq) >= 2:
            q = f"Did you mean {uniq[0].title()} or {uniq[1].title()}?"
        else:
            q = f"Did you want me to open {uniq[0].title()}?"
        logger.info("Fuzzy app: %r → ambiguous %s (conf=%.2f)", name, uniq, conf)
        return _decision(intent="clarification", action="open_app",
                         target=uniq[0], clarification_question=q, confidence=0.55)
    return None


# ── Compound multi-action parser (V7.5b) ─────────────────────────────────────

_COMPOUND_VERBS = re.compile(
    r"\b(open|close|launch|start|run|quit|kill|exit|terminate|"
    r"minimize|minimise|maximize|maximise|restore|focus|"
    r"bring|move|send|put|pull\s+up|switch\s+to|take\s+me\s+to|"
    r"search|play|take|copy|snip|show|visit|go\s+to)\b",
    re.I,
)


# ════════════════════════════════════════════════════════════════════════════
# V14 — Screen-control fast-paths
# Single-shot commands that map cleanly to one screen_control / browser action.
# These bypass the slow agentic brain entirely.
# ════════════════════════════════════════════════════════════════════════════

# Word→digit so "scroll down three times" works
_WORDNUM = {
    "one":1,"a":1,"once":1,"two":2,"twice":2,"three":3,"thrice":3,"four":4,
    "five":5,"six":6,"seven":7,"eight":8,"nine":9,"ten":10,"twenty":20,
    "thirty":30,"forty":40,"fifty":50,"hundred":100,"fewtimes":3,
}

def _parse_amount(text: str, default: int = 5) -> int:
    """V14.4: pull an integer amount from natural phrasing.
    Handles 'three times', '5x', 'a hundred times', '20 ticks'. Capped at 100."""
    m = re.search(r"\b(\d{1,3})\s*(?:times?|x|ticks?)?\b", text)
    if m:
        try: return max(1, min(int(m.group(1)), 100))
        except Exception: pass
    # word numbers — order matters (longer first to avoid 'a' eating everything)
    for w in ("hundred","fifty","forty","thirty","twenty","ten","nine","eight",
              "seven","six","five","four","thrice","three","twice","two","once","one"):
        if re.search(rf"\b{w}(?:\s+times?)?\b", text):
            return _WORDNUM[w]
    return default


# V17: Multi-action splitter ("X and Y" → route each clause)
# Verb words that suggest an action clause
_ACTION_VERBS_RE = re.compile(
    r"\b(?:open|close|focus|minimize|maximize|scroll|click|press|type|"
    r"new\s+tab|close\s+tab|go\s+back|go\s+forward|refresh|reload|"
    r"go\s+to|navigate|visit|switch|search|google|look\s+up|"
    r"select|copy|paste|undo|delete\s+all|clear|"
    r"take\s+a\s+screenshot|describe|read\s+the\s+screen|"
    r"play|pause|stop|mute|"
    r"take\s+me\s+to|bring|show\s+me|focus\s+on|focus|jump\s+to|pull\s+up)\b",
    re.I,
)

def _try_multi_action(text):
    """If `text` contains two clear action-clauses joined by 'and', route each
    separately and combine the results. Returns None if not a multi-action."""
    t = (text or "").strip()
    # Need an explicit conjunction. Don't split commas (too risky — "weather in london, tokyo")
    if not re.search(r"\s+and\s+(?:then\s+)?", t, re.I):
        return None
    parts = re.split(r"\s+and\s+(?:then\s+)?", t, flags=re.I)
    parts = [p.strip().rstrip(".,!?") for p in parts if p.strip()]
    if len(parts) < 2 or len(parts) > 4:
        return None
    # Each part must look like an action (contain an action verb)
    if not all(_ACTION_VERBS_RE.search(p) for p in parts):
        return None
    # Don't fire on "weather in X and Y" style (handled by other paths)
    if re.search(r"\b(?:weather|temperature|time|forecast)\b", t, re.I):
        return None

    results: list[str] = []
    for i, part in enumerate(parts):
        # Try the screen control fast path first
        sc_r = _screen_control_fast_path(part)
        if sc_r:
            results.append(sc_r); continue
        # Then intent router
        try:
            if _intent_router is not None:
                ir_r = _intent_router.route(part)
                if ir_r:
                    results.append(ir_r); continue
        except Exception: pass
        # Couldn't handle this clause — abandon multi-action, let normal pipeline take whole text
        return None
    return " ".join(results) if results else None


def _screen_control_fast_path(text: str) -> str | None:
    """Return a spoken reply if this is a clean single screen-control command.
    Returns None to let the rest of the pipeline (compound parser / AI) handle it."""
    try:
        import screen_control
    except Exception:
        return None
    t = text.lower().strip().rstrip("?.!,;")

    # V14.6 — Bare-chord recognition (NO 'press' prefix needed) ───────────
    # "ctrl a", "ctrl c", "alt tab", "ctrl a and backspace", "ctrl shift t"
    # User-friendly: people don't always say "press X".
    _CHORD_PATTERN = re.compile(
        r"^(?:ctrl|control|alt|shift|win|windows?)\s+"
        r"(?:[a-z0-9]|enter|escape|esc|tab|space|backspace|delete|del|"
        r"home|end|pageup|pagedown|f\d+|"
        r"ctrl|alt|shift)"
        r"(?:\s+(?:and|then|,)?\s*(?:ctrl|alt|shift|win|enter|escape|esc|tab|"
        r"space|backspace|delete|del|home|end|pageup|pagedown|f\d+|[a-z0-9]))*\s*$"
    )
    if _CHORD_PATTERN.match(t):
        # Convert "ctrl shift t" → "ctrl+shift+t"; handles ANY number of modifiers
        # Split on " and " / " then " / "," first to preserve multi-chord sequences
        chords = re.split(r"\s*(?:,| and | then |;)\s*", t)
        out_chords = []
        for ch in chords:
            tokens = re.split(r"\s+", ch.strip())
            normalized = [
                tok.replace("control","ctrl").replace("windows","win").replace("window","win")
                for tok in tokens if tok
            ]
            out_chords.append("+".join(normalized))
        combo = " and ".join(out_chords)
        return screen_control.press_keys(combo)

    # V14.6 — Tab control (browser tabs, not "Tab" the app)
    if re.match(r"^close\s+(?:the\s+|this\s+|current\s+)?tab$", t):
        return screen_control.press_keys("ctrl+w")
    if re.match(r"^close\s+(?:the\s+|this\s+|current\s+)?window$", t):
        return screen_control.press_keys("alt+f4")
    if re.match(r"^(?:reopen|undo\s+close|bring\s+back)\s+(?:the\s+)?(?:last\s+)?"
                r"(?:closed\s+)?tab$", t):
        return screen_control.press_keys("ctrl+shift+t")

    # V14.6 — "select all in/inside/within X" → click_text(X) then ctrl+a
    m = re.match(r"^select\s+(?:all|everything|the\s+(?:text|content))\s+"
                 r"(?:in|inside|within|on)\s+(?:the\s+)?(.+?)$", t)
    if m:
        target = m.group(1).strip().rstrip(".,!?")
        # remove common suffixes
        target = re.sub(r"\s+(only|please|first)$", "", target)
        if target and target not in ("page", "screen", "window"):
            click_r = screen_control.click_text(target)
            if click_r and "couldn't find" not in click_r.lower():
                import time as _t; _t.sleep(0.15)
                sel = screen_control.press_keys("ctrl+a")
                return f"{click_r} Then selected all."
            # fall through to plain Ctrl+A if target not found
        return screen_control.press_keys("ctrl+a")

    # V14.5 — Common editing shortcuts (instant, no vision needed) ────────
    # "select all" / "select everything in the search bar" / etc.
    if re.search(r"\bselect\s+(?:all|everything)\b", t):
        return screen_control.press_keys("ctrl+a")
    # "copy that" / "paste it"
    if re.match(r"^copy(\s+(?:it|that|this|the\s+text))?$", t):
        return screen_control.press_keys("ctrl+c")
    if re.match(r"^paste(\s+(?:it|that|here))?$", t):
        return screen_control.press_keys("ctrl+v")
    if re.match(r"^cut(\s+(?:it|that|this))?$", t):
        return screen_control.press_keys("ctrl+x")
    if re.match(r"^undo(\s+(?:it|that|please))?$", t):
        return screen_control.press_keys("ctrl+z")
    if re.match(r"^redo(\s+(?:it|that))?$", t):
        return screen_control.press_keys("ctrl+y")
    # "clear the search bar" / "delete everything"
    if re.search(r"\b(?:clear|delete|erase|remove)\s+(?:everything|all|the\s+text|"
                 r"the\s+input|the\s+search\s+bar|the\s+field)\b", t):
        return screen_control.press_keys("ctrl+a then delete")

    # V14.5 — Click center / click middle (geometric, no vision) ──────────
    if re.match(r"^click(?:\s+(?:in|on|at))?\s+(?:the\s+)?"
                r"(?:center|middle|centre)(?:\s+of\s+(?:the\s+)?screen)?$", t):
        sw, sh = screen_control.get_screen_size()
        return screen_control.click_at(sw // 2, sh // 2)
    # "click top-left", "click bottom-right" etc.
    _CORNER = {
        "top": (0.5, 0.1), "bottom": (0.5, 0.9),
        "left": (0.1, 0.5), "right": (0.9, 0.5),
        "top-left": (0.1, 0.1), "top-right": (0.9, 0.1),
        "bottom-left": (0.1, 0.9), "bottom-right": (0.9, 0.9),
    }
    m = re.match(r"^click(?:\s+(?:in|on|at))?\s+(?:the\s+)?"
                 r"(top[- ]?left|top[- ]?right|bottom[- ]?left|bottom[- ]?right|"
                 r"top|bottom|left|right)\s*(?:corner|side|of\s+(?:the\s+)?screen)?$", t)
    if m:
        key = m.group(1).replace(" ", "-").replace("--", "-")
        if key in _CORNER:
            sw, sh = screen_control.get_screen_size()
            fx, fy = _CORNER[key]
            return screen_control.click_at(int(sw*fx), int(sh*fy))

    # V14.6 — "go to / switch to / take me to / show me X" → focus_window
    # Prevents the qwen3 catastrophe of close_app("Chrome") on "go to chrome".
    m = re.match(r"^(?:go\s+to|switch\s+to|take\s+me\s+to|jump\s+to|show\s+me|"
                 r"bring\s+up|bring\s+me\s+to|open|launch)\s+(?:the\s+)?(.+?)$", t)
    _BROWSER_TGTS = ("new tab", "a new tab", "new window", "a new window",
                      "new incognito", "incognito tab", "tab", "window")
    # V14.6: only enter this block if the target isn't a browser-action target
    if m and m.group(1).strip().rstrip(".,!?").lower() not in _BROWSER_TGTS:
        tgt = m.group(1).strip().rstrip(".,!?")
        # If it looks like a URL ("youtube.com", "github.com"), let go_to_url
        # handle it (which is matched later). Otherwise try app focus/open.
        if not re.search(r"\.(com|org|net|io|ai|dev|app|co|gov|edu)\b", tgt) \
           and not tgt.startswith(("http", "www.")):
            try:
                import window_tools
                r = window_tools.focus_window(tgt)
                if r and "error" not in r:
                    return f"Brought to front {r.get('title', tgt.title())}."
            except Exception:
                pass
            try:
                import actions
                ar = actions.open_app(tgt)
                if ar: return ar
            except Exception:
                pass

    # ── V14.5 SINGLE-WORD APP — "discord", "chrome", "spotify" alone ──────
    # If the user just says an app name, focus-if-open / open-if-not.
    _APP_WORDS = {
        "discord","chrome","firefox","brave","edge","spotify","steam","slack",
        "vscode","code","notion","obsidian","whatsapp","telegram","zoom","teams",
        "outlook","gmail","youtube","netflix","twitch","github","figma","blender",
        "photoshop","illustrator","obs","valorant","league","minecraft","roblox",
    }
    if t in _APP_WORDS or (len(t.split()) == 1 and t.replace("-", "").isalpha()
                            and t in _APP_WORDS):
        # Try focus first (instant), fall back to open
        try:
            import window_tools
            r = window_tools.focus_window(t)
            if r and "error" not in r:
                return f"Brought to front {r.get('title', t.title())}."
        except Exception:
            pass
        # Not currently open — open it
        try:
            import actions
            return actions.open_app(t)
        except Exception:
            return None

    # ── V14.6 VISION FAST-PATH (broader + filler-stripped) ────────────────
    # Strip leading filler so "what? describe my screen" still matches.
    _filler_stripped = re.sub(
        r"^(?:what|huh|um+|uh+|er+|hmm+|well|so|okay|ok|hey|yo|yeah|"
        r"alright|right|now)\??[\s,.!]+", "", t,
    ).strip()
    _VISION_RE = re.compile(
        r"\b(?:"
        r"what\s+(?:do\s+you|can\s+you)?\s*see\s*(?:on\s+(?:my\s+|the\s+)?screen)?(?:\?|$|\s|,)|"
        r"what(?:'s|\s+is)?\s+(?:that|this|it)?\s*on\s+(?:my|the)\s+screen|"
        r"look\s+at\s+(?:my|the|this)\s+screen|"
        r"see\s+(?:my|the)\s+screen|"
        r"check\s+(?:my|the)\s+screen|"
        r"describe\s+(?:my|the|this)\s+(?:screen|page|window)|"
        r"read\s+(?:this|the\s+screen|what.?s\s+on\s+(?:my|the)\s+screen)|"
        r"tell\s+me\s+what(?:'s|\s+is)?\s+(?:on\s+(?:my|the)\s+screen|here|"
        r"that|this)|"
        r"see\s+what.?s\s+(?:on\s+(?:my|the)\s+)?screen|"
        r"what.?s\s+(?:that|this)\s+(?:on\s+)?(?:my|the)?\s*screen|"
        r"what\s+is\s+(?:it|that|this)\s*\??$|"
        r"what.?s\s+(?:it|that|this)\s*\??$|"
        r"what\s+does\s+(?:it|that|this|the\s+screen)\s+say|"
        r"can\s+you\s+see\s+(?:my|the)\s+screen|"
        r"analyze\s+(?:my|the|this)\s+screen"
        r")\b",
        re.I,
    )
    if _VISION_RE.search(_filler_stripped) or _VISION_RE.search(t):
        try:
            import vision_tools
            return vision_tools.look_at_screen(text)
        except Exception as e:
            logger.warning("vision fast-path failed: %s", e)
            # fall through to agent

    # ── V14.4 CLICK FAST-PATH ─────────────────────────────────────────────
    # "click on X" / "press on X profile" / "click the X button" — these were
    # being mangled by the LLM into press_keys('enter'). Direct route to
    # click_text (which tries UIA first, then vision).
    m = re.match(
        r"^(?:click|press|tap|select)\s+(?:on\s+)?(?:the\s+)?(.+?)"
        r"(?:\s+(?:button|link|icon|tab|profile|option|item))?\s*$", t
    )
    if m and not re.match(r"^(?:press|hit|tap)\s+(?:enter|escape|esc|tab|space|"
                           r"f\d+|ctrl|alt|shift|win|enter\s|escape\s)", t):
        target = m.group(1).strip()
        # Filter out tiny / non-meaningful targets
        if target and len(target) >= 3 and len(target.split()) <= 6:
            try:
                return screen_control.click_text(target)
            except Exception as e:
                logger.warning("click fast-path failed: %s", e)

    # Don't fire on conversational sentences — these patterns are deliberately strict
    # (must start with the verb, no question words, no "what/who/can you").
    if re.match(r"^(what|who|why|how|when|where|can\s+you|could\s+you|please)\b", t):
        # allow "please scroll down" / "can you scroll down" — strip filler
        t2 = re.sub(r"^(please|can\s+you|could\s+you|would\s+you)\s+", "", t).strip()
        if t2 != t and re.match(r"^(scroll|press|type|hit|new\s+tab|close\s+tab|go\s+back|"
                                 r"go\s+forward|refresh|reload|switch\s+tab|reopen)", t2):
            t = t2
        else:
            return None

    # ── Scroll (V14.3: much more permissive) ──────────────────────────────
    # Accepts: "scroll down", "scroll up 3 times", "scroll 5 times down",
    #          "scroll two times upwards", "scroll a bit down", "keep scrolling up"
    if re.search(r"\bscroll(?:ing)?\b", t) or re.match(r"^(?:page|keep\s+going)\b", t):
        # Direction: detect up/down/left/right anywhere in the utterance
        d_m = re.search(r"\b(up|upward|upwards|down|downward|downwards|left|right)\b", t)
        if d_m:
            direction = d_m.group(1)
            if direction in ("upward", "upwards"):       direction = "up"
            if direction in ("downward", "downwards"):   direction = "down"
            amount = _parse_amount(t, default=5)
            return screen_control.scroll(direction, amount)
        # Bare "scroll" / "scroll a bit" / "scroll please" / "scroll twice" → down
        if re.match(r"^scroll(\s+(?:the\s+page|a\s+bit|some|please|more|down)?)?$", t) \
           or re.match(r"^scroll\s+(?:once|twice|thrice|\d+\s+times?)$", t):
            amount = _parse_amount(t, default=5)
            return screen_control.scroll("down", amount)

    # ── Browser actions ───────────────────────────────────────────────────
    if re.match(r"^(?:open\s+(?:a\s+)?new\s+tab|new\s+tab|make\s+(?:a\s+)?new\s+tab|"
                r"add\s+(?:a\s+)?new\s+tab|create\s+(?:a\s+)?new\s+tab)$", t):
        return screen_control.new_tab()
    if re.match(r"^(?:close\s+(?:this\s+)?tab|close\s+tab)$", t):
        return screen_control.close_tab()
    if re.match(r"^(?:reopen|undo\s+close)\s+(?:the\s+)?(?:last\s+)?(?:closed\s+)?tab$", t):
        return screen_control.reopen_tab()
    if re.match(r"^(?:switch|next)\s+tab$", t):
        return screen_control.switch_tab()
    if re.match(r"^(?:go\s+back|back|navigate\s+back|previous\s+page)$", t):
        return screen_control.browser_back()
    if re.match(r"^(?:go\s+forward|forward|next\s+page)$", t):
        return screen_control.browser_forward()
    if re.match(r"^(?:refresh|reload|refresh\s+(?:this\s+)?(?:page|tab))$", t):
        return screen_control.browser_refresh()

    # ── go to URL ─────────────────────────────────────────────────────────
    m = re.match(r"^(?:go\s+to|navigate\s+to|open|visit|take\s+me\s+to)\s+"
                 r"(?:the\s+)?(?:url\s+|website\s+|page\s+)?(.+)$", t)
    if m:
        target = m.group(1).strip().rstrip(".")
        # Convert "youtube dot com" → "youtube.com"
        target = re.sub(r"\s+dot\s+", ".", target)
        # Only treat as URL nav if it LOOKS like a URL or has a TLD
        if re.search(r"[a-z0-9-]+\.(com|org|net|io|ai|dev|app|co|gov|edu|info)\b",
                     target) or target.startswith(("http://", "https://", "www.")):
            if not target.startswith(("http://", "https://")):
                target = "https://" + target.lstrip("www.")
            return screen_control.go_to_url(target)

    # ── Type text ─────────────────────────────────────────────────────────
    # "type X" or "write X" — single-action, send to focused input
    m = re.match(r"^(?:type|write|enter)\s+(?:in\s+|the\s+text\s+)?[\"']?(.+?)[\"']?$", t)
    if m:
        # If the user said "type X in the search bar" — let the agentic brain
        # handle it (needs vision click first). Bare "type X" → fast path.
        if re.search(r"\b(?:in\s+the|into\s+the|on\s+the)\s+(?:search\s+bar|address|input|"
                     r"box|field|dm|chat|message)\b", t):
            return None
        payload = m.group(1).strip()
        if payload and len(payload) <= 200:
            return screen_control.type_text(payload)

    # ── Press keys (V14.3: STRICT — only real key names) ──────────────────
    # Previously "press the search icon" would type "search+icon" as a combo.
    # Now we only fire if every token is a known key/modifier; otherwise we
    # return None so the agentic brain can use click_text/click_at instead.
    _REAL_KEYS = {
        "enter","return","esc","escape","tab","space","spacebar","backspace",
        "delete","del","up","down","left","right","home","end","pageup",
        "page","pagedown","insert","ins","f1","f2","f3","f4","f5","f6","f7",
        "f8","f9","f10","f11","f12","capslock","numlock","printscreen","prtsc",
    }
    _REAL_MODIFIERS = {"ctrl","control","alt","shift","win","windows","cmd"}
    _ALPHA_OK = set("abcdefghijklmnopqrstuvwxyz0123456789")

    m = re.match(r"^(?:press|hit|tap)\s+(?:the\s+)?(.+)$", t)
    if m:
        raw = m.group(1).strip()
        # Normalize separators
        norm = re.sub(r"\bcontrol\b", "ctrl", raw)
        norm = re.sub(r"\bwindows?\s+key\b", "win", norm)
        norm = re.sub(r"\s+(plus|and)\s+", "+", norm)
        norm = re.sub(r"\s+", "+", norm)
        parts = [p.strip().lower() for p in norm.split("+") if p.strip()]
        if not parts: return None
        def _ok(tok: str) -> bool:
            return (tok in _REAL_KEYS or tok in _REAL_MODIFIERS
                    or (len(tok) == 1 and tok in _ALPHA_OK))
        if all(_ok(p) for p in parts):
            return screen_control.press_keys("+".join(parts))
        # Looks like "press the X" where X is a UI element name → defer to agent
        return None

    return None


def _handle_compound(text: str) -> str | None:
    """
    Parse 'X and Y' multi-action commands and execute them in order.
    Resolves 'it'/'that' to the previous action's target.
    Returns the combined reply, or None if this isn't a compound command.
    """
    t = text.lower().strip()
    if " and " not in t:
        return None

    # V11: "search/look up X and tell/show/give me Y" is ONE intent (a search
    # request), not a compound. Don't split — let the agentic brain handle it.
    if re.search(
        r"\b(do\s+a\s+search|search|look\s+(?:it|that|them|something)?\s*up|"
        r"find\s+out|google|look\s+for)\b.*\band\s+(?:tell|show|give|let)\s+me\b",
        t,
    ):
        return None

    # These compound phrases have dedicated single-shot handlers — skip here.
    if re.search(r"screenshot\b.*\band\b.*\bcopy\b", t):
        return None
    if re.search(r"youtube\b.*\band\b.*\bsearch\b", t):
        return None

    parts = [p.strip() for p in re.split(r"\s+and\s+", t) if p.strip()]
    if len(parts) < 2:
        return None
    # First clause must look like an action.
    if not _COMPOUND_VERBS.search(parts[0]):
        return None

    results: list[str] = []
    last_target = None
    for i, part in enumerate(parts):
        # Resolve pronouns to the previous target
        if last_target and re.search(r"\b(it|that|this)\b", part):
            part = re.sub(r"\b(it|that|this)\b", last_target, part)
        # Bare continuation ("open discord and spotify") — inherit verb from
        # part 0, but ONLY for a SHORT bare object (<=2 words). A longer clause
        # with its own structure must NOT inherit — that's how "bring discord
        # to front" wrongly became "close discord" and Maki destroyed the action.
        if i > 0 and not _COMPOUND_VERBS.search(part):
            if len(part.split()) <= 2:
                vm = _COMPOUND_VERBS.search(parts[0])
                if vm:
                    part = f"{vm.group(0)} {part}"
            # else: leave it — _basic_classify returns None → honest "couldn't work out"
        # V14: try the screen-control fast-path before giving up on this part
        sc = _screen_control_fast_path(part)
        if sc is not None:
            results.append(sc)
            continue
        dec = _basic_classify(part)
        if dec is None or dec.get("action") in (None, "", "none"):
            # V14: instead of an honest-but-useless "couldn't work out",
            # delegate this part to the agentic brain so click_text / type_text
            # / vision can handle it.
            try:
                import agent as _ag
                ag_reply = _ag.respond(part.strip())
                if ag_reply:
                    results.append(ag_reply)
                    continue
            except Exception as _e:
                logger.debug("compound: agent delegate failed: %s", _e)
            results.append(f"I couldn't work out \"{part.strip()}\".")
            continue
        if dec.get("intent") == "clarification":
            # Can't resolve a step → stop and ask
            return (" ".join(results) + " " if results else "") + \
                   dec.get("clarification_question", f"I'm not sure about \"{part}\".")
        if dec.get("target"):
            last_target = dec["target"]
        reply = _run_tool(dec)
        memory.set_last_action(dec)
        if reply:
            results.append(reply)
    if not results:
        return None
    return " ".join(results)


# ── Correction / complaint handler (V7.5b) ───────────────────────────────────

def _last_user_question() -> str:
    """Best-effort: the most recent user message that looks like a question."""
    q = memory.get_last_web_search()
    if q:
        return q
    for turn in reversed(memory.get_history()):
        if turn.get("role") == "user":
            c = turn.get("content", "").strip()
            if c and len(c.split()) >= 2:
                return c
    return ""


def _handle_correction(text: str) -> str | None:
    """
    Handle 'you didn't open it', 'nothing happened', 'try again',
    'answer yourself'. Returns a reply, or None if not a correction.
    """
    t = text.lower().strip()

    # ── "answer yourself" → force a direct knowledge answer (no browser) ─────
    if _ANSWER_YOURSELF_RE.search(t):
        q = _last_user_question()
        if not q:
            return "Sure — what would you like me to answer?"
        dec = _decision(intent="safe_action", action="knowledge_query", target=q)
        reply = _run_tool(dec)
        memory.set_last_action(dec)
        return reply

    # ── Correction / complaint → retry the last action ───────────────────────
    if _CORRECTION_RE.search(t):
        last = memory.get_last_action()
        if not last or not last.get("action") or last.get("action") == "none":
            return "I don't have a recent action saved — what would you like me to do?"
        act = last.get("action")
        # knowledge_query that fell back → escalate to a real browser search
        if act == "knowledge_query":
            q = last.get("target", "") or _last_user_question()
            dec = _decision(intent="safe_action", action="search_web", target=q)
            reply = _run_tool(dec)
            memory.set_last_action(dec)
            return f"You're right, let me actually open that. {reply}"
        # Retry app/window/search/etc.
        reply = _run_tool(dict(last))
        return f"You're right, I'll try that again. {reply}"

    return None


# ── Decision factory ──────────────────────────────────────────────────────────

def _decision(**kwargs) -> dict:
    base = {
        "intent":                 "unknown",
        "action":                 "none",
        "target":                 "",
        "query":                  "",
        "confidence":             0.5,
        "needs_clarification":    False,
        "clarification_question": "",
        "spoken_response":        "",
        "tool_needed":            "none",
        "requires_confirmation":  False,
    }
    if kwargs.get("intent") == "clarification":
        kwargs["needs_clarification"] = True
    base.update(kwargs)
    return base


# ── System prompts ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = f"""You are Maki, a voice AI assistant for {config.USER_NAME} on Windows 11.
Your ONLY job: analyse the request and return a JSON decision object.
You do NOT execute anything. Python tools handle all real actions.

RETURN ONLY VALID JSON — no markdown, no text outside the JSON.

Decision schema:
{{
  "intent": "safe_action | risky_action | conversation | clarification | acknowledgement | unknown",
  "action": "get_current_time | get_current_date | get_time_in | calculate_day_of_date |
             get_disk_space | open_app | close_app | sleep_pc | check_process |
             open_website | open_named_site | search_google | search_youtube | search_web |
             open_folder | get_current_mode_and_model | get_permissions | none",
  "target": "",
  "query": "",
  "confidence": 0.95,
  "needs_clarification": false,
  "clarification_question": "",
  "spoken_response": "",
  "tool_needed": "none | datetime | disk | process | app_control | webbrowser | web_search | system_info",
  "requires_confirmation": false
}}

RULES (strict):
1. conversation → action="none", write natural 1-3 sentence spoken_response. NEVER invent time/date/live facts.
2. safe_action → correct action + target. Leave spoken_response empty.
3. risky_action (sleep, delete, etc.) → requires_confirmation=true.
4. unclear → clarification intent, write clarification_question.
5. confidence < 0.75 → needs_clarification=true.
6. "is X running / open / active" → check_process, target=app name.
7. LIVE/CURRENT INFO only → search_web: weather, stock prices, live scores, today's breaking news,
   "right now / as of today" rankings. General knowledge ("best X", "how does X work", coding,
   history, advice, explanations) → conversation, answer from training data. Do NOT over-search.
8. "play/watch X on YouTube" → search_youtube.
9. "play X on Spotify" → open_app target=spotify.
10. Date questions → calculate_day_of_date, target=date string. Python does math, not you.
11. "riot/riot games" → open_app target=riot client.  "league/lol" → open_app target=league of legends.
12. "code/vscode/visual studio" → open_app target=vs code.
13. "okay / got it / alright / cool" with no pending action → acknowledgement intent,
    spoken_response = "Got it." or "Alright." (never replay previous action).
14. spoken_response: warm, short, natural, voice-friendly. No "Great question!" No bullet points.
    Sound like a knowledgeable friend, not a corporate assistant.

Available apps: spotify, discord, chrome, edge, firefox, league of legends, valorant,
riot client, rocket league, steam, docker, vs code, notepad, calculator, photos,
word, excel, powerpoint, nvidia, geforce experience, epic games, task manager, camera.

Available sites: youtube, gmail, google, github, reddit, twitter, chatgpt, claude, n8n.
"""

_CHAT_SYSTEM = f"""You are Maki, {config.USER_NAME}'s personal AI assistant running on his Windows 11 PC.
You're like a smart, calm, confident friend — knowledgeable, helpful, and natural.

VOICE PERSONALITY (critical):
- Keep responses SHORT for voice: 1-3 sentences max (unless asked for detail)
- Warm, direct, occasionally witty — not corporate or robotic
- Use contractions naturally: I'm, you're, can't, that's, don't
- NEVER start with: "Great question!", "Certainly!", "Of course!", "Absolutely!", "Sure thing!"
- NEVER say "I am an AI language model" or "As an AI..."
- No bullet points in spoken responses — use natural flowing sentences
- Don't end every reply with "Is there anything else I can help you with?"

WHAT YOU SHOULD ANSWER DIRECTLY (from your training knowledge):
- General knowledge: history, science, tech explanations, coding help
- "What is the best X?" — give a confident, helpful answer from what you know
- Advice, recommendations, opinions when asked
- How things work, concepts, comparisons
- Conversation, emotional support, casual chat

WHAT PYTHON TOOLS HANDLE (never invent these):
- Current time, date → Python
- Disk space, system info → Python
- Opening/closing apps → Python
- Running process list → Python

WHAT NEEDS LIVE SEARCH (truly time-sensitive only):
- Today's weather, current prices, live scores, breaking news
- Explicitly "right now / currently / as of today" rankings
- For these: "Want me to open a live search for that?"

EXAMPLES OF GOOD RESPONSES:
User: "How are you?" → "I'm good, {config.USER_NAME}. Ready when you are."
User: "I'm tired." → "Yeah, I get that. Want me to put on something relaxing?"
User: "What's the best AI model?" → "Right now GPT-4o, Claude, and Gemini are all top contenders depending on the task. For the absolute latest rankings I can search it up."
User: "What is the best AI model currently?" → "Rankings shift fast — want me to open a live search?"
User: "Explain how neural networks work." → Give a clear 2-3 sentence explanation.
User: "Thanks." → "Anytime!"
User: "No that's fine." → "Alright."
User: "Okay." → "Got it."

User's name: {config.USER_NAME}. He's likely in Pakistan. Be natural and conversational.
"""


# ── Gemini helpers ────────────────────────────────────────────────────────────

def _handle_gemini_error(e: Exception):
    global _gemini_ok, _gemini_fail_reason, _gemini_retry_after
    err = str(e).lower()
    if any(k in err for k in ("quota", "rate", "429", "resource exhausted", "limit")):
        _gemini_fail_reason = "rate limit"
        # V13: per-minute RPM limit recovers in ~60s; daily quota in ~1h.
        # Distinguish: "quota" / "daily" → 1h, plain "rate"/"429" → 75s.
        if any(k in err for k in ("quota", "daily", "exceeded your current quota")):
            _gemini_retry_after = time.monotonic() + 3600
            logger.warning("Gemini daily quota exhausted — 1-hour cooldown.")
        else:
            _gemini_retry_after = time.monotonic() + 75
            logger.warning("Gemini per-minute rate limit — 75s cooldown.")
        # Don't flip _gemini_ok — key is still valid; just cooling down
    elif any(k in err for k in ("api_key", "invalid", "unauthorized", "403", "api key", "invalid key")):
        _gemini_fail_reason = "invalid API key"
        _gemini_ok = False       # Permanent for this session
        logger.error("Gemini API key invalid — disabling for this session.")
        if get_mode() == MODE_GEMINI:
            _set_mode(MODE_OLLAMA if check_ollama() else MODE_BASIC)
    else:
        _gemini_fail_reason = str(e)[:80]
        logger.debug("Gemini transient error (not disabling): %s", e)


def _to_gemini_history(hist: list) -> list:
    """
    Convert OpenAI-style history → google-genai Content list.
    Merges consecutive same-role messages (Gemini requires alternating).
    Strips trailing user turns (they become the next send_message call).
    """
    from google.genai import types
    result: list = []
    for msg in hist:
        role    = "model" if msg["role"] == "assistant" else "user"
        content = msg.get("content", "").strip()
        if not content:
            continue
        if result and result[-1].role == role:
            # Merge consecutive same-role
            prev_text = result[-1].parts[0].text
            result[-1] = types.Content(
                role=role, parts=[types.Part(text=prev_text + " " + content)]
            )
        else:
            result.append(types.Content(role=role, parts=[types.Part(text=content)]))
    # Gemini chat history must end with a model turn
    while result and result[-1].role == "user":
        result.pop()
    return result


_GEMINI_TIMEOUT = getattr(config, "GEMINI_TIMEOUT_SECONDS", 6)

# V9: cache the genai client — rebuilding it on every call added needless latency.
_genai_client = None
_genai_client_lock = threading.Lock()


def _get_genai_client():
    global _genai_client
    if _genai_client is None:
        with _genai_client_lock:
            if _genai_client is None:
                from google import genai
                _genai_client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _genai_client


def _ask_gemini_decision(text: str) -> dict | None:
    if not _can_use_gemini():
        return None

    def _call():
        from google.genai import types
        client    = _get_genai_client()
        hist_text = ""
        recent    = memory.get_recent_text(n=4)
        if recent:
            hist_text = f"Recent conversation:\n{recent}\n\n"
        prompt   = f"{hist_text}Classify this user request:\n{text}"
        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0.1,
                max_output_tokens=400,
            ),
        )
        raw  = response.text.strip()
        data = json.loads(raw)
        return _decision(**{k: v for k, v in data.items() if k in _decision()})

    try:
        with _futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_call)
            return fut.result(timeout=_GEMINI_TIMEOUT)
    except _futures.TimeoutError:
        logger.warning("Gemini decision timed out (%ds) — falling back.", _GEMINI_TIMEOUT)
        return None
    except Exception as e:
        _handle_gemini_error(e)
        return None


def _chat_gemini(text: str) -> str:
    if not _can_use_gemini():
        return ""

    def _call():
        from google.genai import types
        client = _get_genai_client()
        hist   = memory.get_history()
        g_hist = _to_gemini_history(hist)
        cfg    = types.GenerateContentConfig(
            system_instruction=_CHAT_SYSTEM,
            temperature=0.7,
            max_output_tokens=300,
        )
        try:
            chat     = client.chats.create(model=config.GEMINI_MODEL, config=cfg, history=g_hist)
            response = chat.send_message(text)
        except Exception:
            response = client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=text,
                config=cfg,
            )
        return response.text.strip()

    try:
        with _futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_call)
            return fut.result(timeout=_GEMINI_TIMEOUT)
    except _futures.TimeoutError:
        logger.warning("Gemini chat timed out (%ds) — falling back.", _GEMINI_TIMEOUT)
        return ""
    except Exception as e:
        _handle_gemini_error(e)
        return ""


# ── Ollama helpers ────────────────────────────────────────────────────────────

# V7.5b: persistent executor so a slow Ollama call can be ABANDONED.
# We never block on its shutdown — a stuck worker just leaks until the HTTP
# request's own timeout fires, but the caller has already moved on.
_OLLAMA_HARD_TIMEOUT  = 5.5   # absolute wall-clock ceiling for any Ollama path
_OLLAMA_SLOW_COOLDOWN = 180   # after a timeout, skip Ollama entirely for 3 min
_ollama_pool = _futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="ollama")
_ollama_slow_until = 0.0      # monotonic ts; if now < this, Ollama is skipped
_ollama_slow_logged = 0.0     # rate-limit the "in cooldown" log line


def _ollama_healthy() -> bool:
    """
    False if Ollama recently timed out. V9 fix: a single 5.5s timeout was
    happening on BOTH the decision call AND the chat call = 11s hangs, every
    turn while Gemini was rate-limited. Now one timeout sidelines Ollama for
    3 minutes so subsequent turns skip it instantly.
    """
    global _ollama_slow_until, _ollama_slow_logged
    if not _ollama_ok:
        return False
    if _ollama_slow_until > 0:
        now = time.monotonic()
        if now < _ollama_slow_until:
            if now - _ollama_slow_logged > 30:
                logger.info("Ollama skipped — in slow-cooldown (%ds left).",
                            int(_ollama_slow_until - now))
                _ollama_slow_logged = now
            return False
        _ollama_slow_until = 0.0
        logger.info("Ollama slow-cooldown expired — re-enabling.")
    return True


def _ollama_with_timeout(fn, *args):
    """Run fn in the pool with a hard wall-clock ceiling. Returns fn() or None."""
    global _ollama_slow_until
    if not _ollama_healthy():
        return None
    try:
        fut = _ollama_pool.submit(fn, *args)
    except Exception as e:
        logger.debug("Ollama pool submit failed: %s", e)
        return None
    try:
        return fut.result(timeout=_OLLAMA_HARD_TIMEOUT)
    except _futures.TimeoutError:
        _ollama_slow_until = time.monotonic() + _OLLAMA_SLOW_COOLDOWN
        logger.info("Ollama timeout — sidelining it for %ds (fast fallback used).",
                    _OLLAMA_SLOW_COOLDOWN)
        return None
    except Exception as e:
        logger.debug("Ollama call error: %s", e)
        return None


def _ask_ollama_decision_raw(text: str) -> dict | None:
    hist = memory.get_history()
    messages = (
        [{"role": "system", "content": _SYSTEM_PROMPT}]
        + hist
        + [{"role": "user", "content": text}]
    )
    _model = _ollama_model_actual or config.OLLAMA_MODEL
    # requests timeout slightly under the hard ceiling
    _otimeout = min(getattr(config, "OLLAMA_TIMEOUT_SECONDS", 5), 5)
    try:
        r = requests.post(config.OLLAMA_URL, json={
            "model":    _model,
            "messages": messages,
            "stream":   False,
            "format":   "json",
        }, timeout=_otimeout)
        r.raise_for_status()
        raw  = r.json().get("message", {}).get("content", "")
        data = json.loads(raw)
        return _decision(**{k: v for k, v in data.items() if k in _decision()})
    except requests.ConnectionError:
        logger.warning("Ollama unavailable, using Basic Mode.")
        _set_mode(MODE_BASIC)
        return None
    except Exception as e:
        logger.debug("Ollama decision error: %s", e)
        return None


def _ask_ollama_decision(text: str) -> dict | None:
    """Hard-timeout-wrapped Ollama decision call."""
    return _ollama_with_timeout(_ask_ollama_decision_raw, text)


def _chat_ollama_raw(text: str) -> str:
    hist = memory.get_history()
    messages = (
        [{"role": "system", "content": _CHAT_SYSTEM}]
        + hist
        + [{"role": "user", "content": text}]
    )
    _model = _ollama_model_actual or config.OLLAMA_MODEL
    _otimeout = min(getattr(config, "OLLAMA_TIMEOUT_SECONDS", 5), 5)
    try:
        r = requests.post(config.OLLAMA_URL, json={
            "model":    _model,
            "messages": messages,
            "stream":   False,
        }, timeout=_otimeout)
        r.raise_for_status()
        return r.json().get("message", {}).get("content", "").strip()
    except Exception as e:
        logger.debug("Ollama chat error: %s", e)
        return ""


def _chat_ollama(text: str) -> str:
    """
    V7.5b: hard 5.5s wall-clock ceiling via futures. If Ollama is slow,
    we ABANDON it (don't wait for the underlying HTTP call) and return "".
    """
    result = _ollama_with_timeout(_chat_ollama_raw, text)
    return result or ""


# ── AI dispatcher ─────────────────────────────────────────────────────────────

def _ask_ai_decision(text: str) -> dict | None:
    """Gemini → Ollama → None."""
    if _can_use_gemini():
        dec = _ask_gemini_decision(text)
        if dec is not None:
            return dec
        logger.info("Gemini unavailable or in cooldown — falling back to Ollama.")
    if get_mode() in (MODE_OLLAMA, MODE_GEMINI):
        return _ask_ollama_decision(text)
    return None


# Heuristic: short, simple, emotional/casual messages → Ollama is plenty.
# Long, complex, reasoning-heavy messages → use Gemini for quality.
_COMPLEX_HINT = re.compile(
    r"\b("
    r"explain|how\s+(do|does|can|would|should)|why\s+(do|does|is|are)|"
    r"compare|difference\s+between|pros\s+and\s+cons|step.by.step|"
    r"write\s+(code|a\s+\w+)|help\s+me\s+(write|debug|build|design|study|understand)|"
    r"what\s+does\s+this\s+(code|error|mean)|"
    r"recipe|formula|theorem|integral|derivative|calculus|algebra"
    r")\b",
    re.I,
)


def _looks_complex(text: str) -> bool:
    """True if the message likely benefits from Gemini's deeper reasoning."""
    if _COMPLEX_HINT.search(text):
        return True
    # Long messages or multi-clause sentences → Gemini
    if len(text.split()) > 18:
        return True
    if text.count(",") >= 2 or text.count(";") >= 1:
        return True
    return False


def _chat_ai(text: str) -> str:
    """
    V7.5b router — Ollama is NEVER the default, only a hard-capped fallback.
      1. Gemini if available (has its own 5s timeout)
      2. Ollama ONCE, hard 5.5s ceiling — abandoned if slow
      3. "" → caller supplies a short natural fallback
    """
    # 1. Gemini first whenever it's usable
    if _can_use_gemini():
        resp = _chat_gemini(text)
        if resp:
            return resp
        logger.info("Gemini empty/failed — trying Ollama once (hard-capped).")

    # 2. Ollama as a single hard-capped fallback (cooldown or Gemini-empty)
    if _ollama_ok:
        resp = _chat_ollama(text)
        if resp:
            return resp

    # 3. Nothing — caller handles the short fallback line
    return ""


# ── Tool executor ─────────────────────────────────────────────────────────────

def _run_tool(decision: dict) -> str:
    global _last_tool
    action = decision.get("action", "none")
    target = decision.get("target", "").strip()
    query  = decision.get("query",  "").strip()
    _last_tool = action

    if action == "get_weather_multi":
        if not target:
            return "Which cities should I check?"
        if weather_tools is None:
            return "Weather lookup isn't installed."
        cities = [c.strip() for c in target.split("||") if c.strip()]
        parts: list[str] = []
        last_ok = None
        for city in cities[:5]:
            r = weather_tools.get_weather(city)
            if "error" in r:
                parts.append(f"Couldn't find weather for {city.title()}.")
            else:
                parts.append(r["summary"])
                last_ok = r
        # Remember the LAST successful city for "convert to celsius" follow-up
        try:
            if last_ok:
                memory.set_last_weather(
                    float(last_ok["temp"]),
                    "F" if "°F" in last_ok.get("unit", "") else "C",
                    last_ok.get("location", cities[-1] if cities else ""),
                )
        except Exception:
            pass
        return " ".join(parts) if parts else "Couldn't check those cities."

    if action == "get_weather":
        if not target:
            return "Which city should I check?"
        if weather_tools is None:
            return "Weather lookup isn't installed. I can open a search instead — just ask."
        result = weather_tools.get_weather(target)
        if "error" in result:
            return (f"I couldn't get live weather for {target.title()}. "
                    f"Want me to open a search instead?")
        # Remember temp + location for F↔C follow-up
        try:
            memory.set_last_weather(
                float(result["temp"]),
                "F" if "°F" in result.get("unit", "") else "C",
                result.get("location", target),
            )
        except Exception:
            pass
        return result["summary"]

    if action == "convert_temp":
        last = memory.get_last_weather()
        if not last:
            return "I don't have a recent temperature to convert."
        temp, unit, loc = last["temp"], last["unit"], last["location"]
        if target == "C" and unit == "F":
            c = round((temp - 32) * 5 / 9, 1)
            return f"{round(temp)}°F in {loc} is about {c}°C."
        if target == "F" and unit == "C":
            f = round((temp * 9 / 5) + 32, 1)
            return f"{round(temp)}°C in {loc} is about {f}°F."
        return f"That's already in {unit}."

    if action == "get_current_time":
        t = tools.get_current_time()
        return f"It's {t}."

    if action == "get_time_multi":
        if not world_time_tools:
            return "World-time tool unavailable."
        parts = []
        for place in (target or "").split("||"):
            place = place.strip()
            if not place: continue
            parts.append(world_time_tools.speak_time_in(place))
        return " ".join(parts) if parts else "Couldn't check those places."

    if action == "get_time_in":
        if world_time_tools:
            return world_time_tools.speak_time_in(target)
        # Legacy fallback
        result = tools.get_time_in(target)
        if "error" in result:
            return (f"I don't know the timezone for {target}. "
                    f"Try cities like Dubai, London, New York, or Karachi.")
        return f"It's {result['time']} in {result['location']}."

    if action == "get_current_date":
        return f"Today is {tools.get_current_date()}."

    if action == "calculate_day_of_date":
        result = tools.calculate_day_of_date(target)
        if "error" in result:
            return "Couldn't parse that date — try 'May 27' or 'June 3rd 2026'."
        resp = f"{result['date']} is a {result['day_of_week']}."
        if result.get("assumed_year"):
            resp = f"Assuming {result['year']}, {result['date']} is a {result['day_of_week']}."
        return resp

    if action == "get_folder_size":
        if not target:
            return "Which folder should I measure?"
        result = tools.get_folder_size(target)
        if "error" in result:
            return result["error"]
        return (f"The {target} folder takes {result['size']} "
                f"across {result['files']} files.")

    if action == "get_game_size":
        if not target:
            return "Which game should I check?"
        result = tools.get_game_size(target)
        if "error" in result:
            return result["error"]
        return f"{result['name']} is using about {result['size']}."

    if action == "get_largest_folders":
        result = tools.get_largest_folders(target or "")
        if "error" in result:
            return result["error"]
        folders = result.get("folders", [])
        if not folders:
            return "I didn't find any sub-folders to measure there."
        top = folders[0]
        rest = ", ".join(f"{f['name']} ({f['size']})" for f in folders[1:4])
        msg = f"Biggest is {top['name']} at {top['size']}"
        if rest:
            msg += f", then {rest}"
        return msg + "."

    if action == "get_disk_space":
        drive  = target or "C"
        result = tools.get_disk_space(drive)
        if "error" in result:
            return f"Couldn't read the {drive} drive."
        return (f"Your {result['drive']} drive has {result['free_gb']} GB free "
                f"of {result['total_gb']} GB — {result['pct_free']}% free.")

    if action in ("get_current_mode_and_model", "get_provider_status"):
        return _mode_response()

    if action == "recall_memory":
        if not target:
            recent = memory.get_recent_text(n=4).strip()
            if not recent:
                return "We haven't talked about anything yet — what's on your mind?"
            return "Recently we covered — " + recent.replace("\n", "; ")
        hits = memory.search_history(target, limit=5)
        if not hits:
            return f"I don't see anything in our history about '{target}'."
        snippets = []
        for h in hits[-3:]:
            who = "you" if h.get("role") == "user" else "I"
            content = (h.get("content", "") or "").strip()
            if content:
                snippets.append(f'{who} said "{content}"')
        if not snippets:
            return f"I found a reference to '{target}' but nothing quotable."
        return "Here's what I found — " + "; ".join(snippets) + "."

    if action == "get_permissions":
        return (
            "I can open and close apps, search Google and YouTube, open websites, "
            "check disk space and running processes, tell you the time anywhere, "
            "do date calculations, and have a real conversation. "
            "Risky stuff like deleting files or sending messages needs your explicit yes first."
        )

    if action == "add_time_offset":
        # target format: "2h0m" or "0h30m"
        hrs_m = re.search(r"(\d+)h(\d+)m", target)
        if not hrs_m:
            return "I couldn't parse that time offset."
        hrs  = int(hrs_m.group(1))
        mins = int(hrs_m.group(2))
        t_str = tools.add_time_offset(hours=hrs, minutes=mins)
        parts = []
        if hrs:
            parts.append(f"{hrs} hour{'s' if hrs != 1 else ''}")
        if mins:
            parts.append(f"{mins} minute{'s' if mins != 1 else ''}")
        label = " and ".join(parts)
        return f"In {label}, it will be {t_str}."

    if action == "list_running_apps":
        # V7: window_tools gives a more complete picture (includes Claude, browser-tab apps)
        if window_tools:
            running = window_tools.list_running_apps()
        else:
            running = tools.list_running_common_apps().get("running", [])
        if not running:
            return "I didn't detect any common apps running right now."
        return f"Running right now: {', '.join(running)}."

    if action == "count_running_apps":
        if window_tools:
            running = window_tools.list_running_apps()
        else:
            running = tools.list_running_common_apps().get("running", [])
        n = len(running)
        if n == 0:
            return "I don't see any common apps running right now."
        # Short preview: first 6
        preview = ", ".join(running[:6])
        more = f" and {n-6} more" if n > 6 else ""
        return f"{n} app{'s' if n != 1 else ''} running — {preview}{more}."

    # ── Screenshot / snipping handlers (V7.5) ─────────────────────────────────
    if action == "take_screenshot":
        if not screenshot_tools:
            return "Screenshot tools aren't installed."
        r = screenshot_tools.take_screenshot(copy=False)
        if "error" in r:
            return r["error"]
        memory.set_last_screenshot(r["path"])
        return f"Got it. Saved the screenshot."

    if action == "take_screenshot_clipboard":
        if not screenshot_tools:
            return "Screenshot tools aren't installed."
        r = screenshot_tools.take_screenshot_to_clipboard()
        if "error" in r:
            return r["error"]
        memory.set_last_screenshot(r["path"])
        if r.get("copied"):
            return "Done — screenshot saved and copied to your clipboard."
        return f"Screenshot saved, but I couldn't copy it to the clipboard."

    if action == "open_snipping_tool":
        if not screenshot_tools:
            return "Screenshot tools aren't installed."
        r = screenshot_tools.open_snipping_tool()
        if "error" in r:
            return r["error"]
        memory.set_pending_snip(True)
        return r["result"]

    if action == "snip_area_manual":
        if not screenshot_tools:
            return "Screenshot tools aren't installed."
        r = screenshot_tools.snip_area_manual()
        if "error" in r:
            return r["error"]
        memory.set_pending_snip(True)
        return r["result"]

    if action == "save_clipboard_image":
        if not screenshot_tools:
            return "Screenshot tools aren't installed."
        r = screenshot_tools.save_clipboard_image()
        memory.set_pending_snip(False)
        if "error" in r:
            return r["error"]
        memory.set_last_screenshot(r["path"])
        return f"Saved the snip from your clipboard."

    if action == "copy_last_screenshot":
        if not screenshot_tools:
            return "Screenshot tools aren't installed."
        r = screenshot_tools.copy_last_screenshot()
        if "error" in r:
            return r["error"]
        return "Copied the last screenshot to your clipboard."

    if action == "open_screenshot_folder":
        if not screenshot_tools:
            return "Screenshot tools aren't installed."
        r = screenshot_tools.open_screenshot_folder()
        return r.get("result") or r.get("error", "Couldn't open the folder.")

    if action in ("minimize_window", "maximize_window", "restore_window", "focus_window"):
        if not window_tools:
            return "Window control isn't available — pywin32 may be missing."
        if not target:
            return "Which window should I act on?"
        fn = {
            "minimize_window": window_tools.minimize_window,
            "maximize_window": window_tools.maximize_window,
            "restore_window":  window_tools.restore_window,
            "focus_window":    window_tools.focus_window,
        }[action]
        result = fn(target)
        if "error" in result:
            # V9: "take me to X" / "focus X" — if the window isn't open, OPEN it
            # — BUT only if the target is a recognized app. Otherwise we'd
            # blindly try to launch garbled junk like "this core" (V11 fix).
            if action == "focus_window":
                is_known = False
                if app_index is not None:
                    try:
                        r = app_index.resolve(target)
                        is_known = (r.get("confidence", 0) >= 0.85)
                        if is_known and r.get("match"):
                            target = r["match"]   # use the canonical name
                    except Exception:
                        pass
                if is_known:
                    logger.info("focus_window: no window for %r — opening %r instead.",
                                target, target)
                    open_dec = _decision(intent="safe_action", action="open_app", target=target)
                    return _run_tool(open_dec)
                # Unknown app — don't launch garbage. Be honest.
                return (f"I don't see a window for '{target}', and I'm not sure that's "
                        f"a real app on this PC. Did you mean a different name?")
            return result["error"]
        verb_past = {
            "minimize_window": "Minimized",
            "maximize_window": "Maximized",
            "restore_window":  "Restored",
            "focus_window":    "Brought to front",
        }[action]
        # V9: tidy the displayed title — collapse long browser titles
        title = result.get("title", target)
        if len(title) > 50:
            title = title[:47].rsplit(" ", 1)[0] + "…"
        return f"{verb_past} {title}."

    if action == "check_process":
        if not target:
            return "Which app would you like me to check?"
        display = re.sub(r"\s+(is|are)\s*$", "", target, flags=re.I).strip().title()
        # V7: prefer window_tools (catches Claude, browser tabs, more apps)
        if window_tools and window_tools.is_app_running(target):
            return f"Yeah, {display} is running."
        result = tools.check_process(target)
        if "error" in result:
            return f"No, I don't see {display} running."
        running = result.get("running", False)
        return (f"Yeah, {display} is running." if running
                else f"No, {display} doesn't seem to be running right now.")

    if action == "open_app":
        if not target:
            return "Which app would you like me to open?"
        result   = tools.open_app(target)
        res_text = result.get("result", "")
        err_sig  = ("couldn't", "wasn't found", "not found", "failed", "make sure", "add its path")
        launched_via_shell = False
        if any(s in res_text.lower() for s in err_sig):
            # Last-resort: Windows shell start
            try:
                import subprocess
                subprocess.Popen(["cmd", "/c", "start", target], shell=False)
                launched_via_shell = True
            except Exception:
                return (f"I tried opening {target.title()}, but couldn't find it. "
                        f"Make sure it's installed or add its path to config.py.")
        # ── Discord verification (wait + process check) ────────────────────────
        if target.lower() == "discord":
            time.sleep(2.5)
            check = tools.check_process("discord")
            if check.get("running"):
                return "Discord is open."
            # One more try via shell if we haven't yet
            if not launched_via_shell:
                try:
                    import subprocess
                    subprocess.Popen(["cmd", "/c", "start", "discord"], shell=False)
                    time.sleep(3)
                    check2 = tools.check_process("discord")
                    if check2.get("running"):
                        return "Discord is open now."
                except Exception:
                    pass
            return ("I tried opening Discord but it doesn't appear to be running. "
                    "It might still be loading, or you may need to open it manually.")
        if launched_via_shell:
            return f"Opened {target.title()} via Windows."
        return f"Sure, opening {target.title()}."

    if action == "close_app":
        if not target:
            return "Which app would you like me to close?"
        result   = tools.close_app(target)
        res_text = result.get("result", "")
        err_sig  = ("isn't running", "couldn't", "not found", "failed", "no process")
        if any(s in res_text.lower() for s in err_sig):
            return res_text
        return f"Done, closed {target.title()}."

    if action == "close_browser_window":
        # Find a window whose title contains the keyword (e.g. "Gmail")
        if not window_tools:
            return "Window control isn't available."
        kw = (target or "").lower()
        if not kw:
            return "Close which browser window?"
        # Try to minimize (safer than killing the whole browser process)
        found = window_tools._find_hwnd_for(kw)
        if not found:
            return f"I don't see a browser window with {target.title()}."
        # We won't kill the parent browser (would close other tabs). Minimize instead
        # and tell the user honestly.
        r = window_tools.minimize_window(kw)
        if "error" in r:
            return r["error"]
        return (f"I minimized the {target.title()} window. I can close the whole browser, "
                f"but I can't close a single tab safely yet — say 'close chrome' if you want that.")

    if action == "open_powershell_admin":
        # Launch an elevated PowerShell via the Start-Process -Verb RunAs trick.
        try:
            import subprocess
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command",
                 "Start-Process powershell -Verb RunAs"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return "Opening PowerShell as administrator — accept the UAC prompt."
        except Exception as e:
            logger.warning("Admin PowerShell launch failed: %s", e)
            return "I couldn't launch an elevated PowerShell. Try opening it manually."

    if action == "sleep_pc":
        result = tools.sleep_pc()
        return result["result"]

    if action == "search_youtube":
        q = target or query
        if not q:
            return "What would you like to search for on YouTube?"
        tools.search_youtube(q)
        return f"Searching YouTube for '{q}'."

    if action == "search_google":
        q = target or query
        if not q:
            return "What would you like to search on Google?"
        tools.search_google(q)
        return f"Searching Google for '{q}'."

    if action == "knowledge_query":
        # V7.5b: Gemini → live_lookup (Tavily/Brave/DDG/Wikipedia) → honest fallback.
        # NEVER Ollama (too slow). NEVER browser by default.
        q = target or query
        if not q:
            return "What would you like to know about?"
        # 1. Gemini if available (has its own 5s timeout)
        if _can_use_gemini():
            resp = _chat_gemini(q)
            if resp:
                return resp
        # 2. Live lookup — answers in-app from free APIs
        if web_tools:
            hit = web_tools.live_lookup(q)
            if hit.get("answer"):
                ans  = hit["answer"]
                if len(ans) > 360:
                    ans = ans[:360].rsplit(".", 1)[0] + "."
                src  = hit.get("source", "")
                kind = hit.get("kind", "")
                # Cite real sources (Tavily/Brave/Wikipedia); skip noise for bare factoids
                if src and kind in ("Tavily", "Brave", "Wikipedia"):
                    return f"{ans}  —  {src}"
                return ans
        # 3. Short honest fallback — offer search, never an internal status dump
        memory.set_pending_web_search(q)
        return (f"I couldn't pin that down just now — want me to open a live "
                f"search for '{q}'?")

    if action == "search_web":
        q = target or query
        if not q:
            return "What would you like me to search for?"
        # V7: Try to ANSWER inside the app first via free APIs.
        if web_tools:
            hit = web_tools.live_lookup(q)
            if hit.get("answer"):
                ans  = hit["answer"]
                # Keep voice replies brief — trim very long Wikipedia extracts.
                if len(ans) > 360:
                    ans = ans[:360].rsplit(".", 1)[0] + "."
                src  = hit.get("source", "")
                kind = hit.get("kind", "")
                memory.set_last_web_search(q)
                if src and kind in ("Tavily", "Brave", "Wikipedia"):
                    return f"{ans}  —  {src}"
                return ans
        # Fall back to browser only when the lookup failed.
        tools.search_web(q)
        memory.set_last_web_search(q)
        return (f"I couldn't fetch that directly — I opened a live search for '{q}' instead.")

    if action == "open_named_site":
        tools.open_named_site(target)
        return f"Sure, opening {target.title()}."

    if action == "open_website":
        url = target or query
        tools.open_website(url)
        return f"Opening {url}."

    if action == "open_folder":
        result = tools.open_folder(target)
        return result["result"]

    _last_tool = "none"
    return ""


# ── Mode identity response ────────────────────────────────────────────────────

def _mode_response() -> str:
    """Always report real provider state — both Gemini and Ollama if available."""
    # V7.5b: if Gemini is in cooldown, say so plainly.
    if _gemini_ok and _gemini_retry_after > 0 and time.monotonic() < _gemini_retry_after:
        secs = max(0, int(_gemini_retry_after - time.monotonic()))
        mins = max(1, secs // 60)
        return (f"Gemini is currently in cooldown because of rate limits "
                f"(about {mins} minute{'s' if mins != 1 else ''} left), so I'm using "
                f"Python tools and local fallback until it's available again.")
    ollama_model = _ollama_model_actual or config.OLLAMA_MODEL
    return (f"I'm using Gemini {config.GEMINI_MODEL} as the main reasoning brain, "
            f"Ollama {ollama_model} as local backup, and Python tools for direct "
            f"actions like time, weather, apps, screenshots, and windows.")


# ── Confirmation question ─────────────────────────────────────────────────────

def _confirm_question(decision: dict) -> str:
    action = decision.get("action", "")
    target = (decision.get("target", "") or "").strip()
    if action == "sleep_pc":
        return "Do you want me to put the PC to sleep? Say yes or no."
    if action == "close_app":
        return f"Do you want me to close {target.title() or 'that app'}? Say yes or no."
    if action == "open_app":
        return f"Do you want me to open {target.title() or 'that app'}? Say yes or no."
    verb = action.replace("_", " ") if action and action != "none" else "do that"
    obj  = f" {target}" if target else ""
    return f"Should I go ahead and {verb}{obj}? Say yes or no."


# ── Safety ────────────────────────────────────────────────────────────────────

def _is_risky_transcript(text: str) -> bool:
    return safety.is_risky(text)


# ── Basic-mode social / small-talk responses (instant, no AI) ────────────────

_SOCIAL = [
    # "how are you" — fuzzy to handle STT/typo variations.
    # V19 BUG-D FIX: previous regex `how.{0,8}(are|r)` matched "how OLD are
    # you" (4 chars between "how" and "are"). New regex requires "how" to
    # be directly followed by an "are"-variant with only short whitespace
    # or a contraction in between. Variants like "how old are you" or
    # "how tall are you" now fall through to the chat lane for a real answer.
    (re.compile(
        r"\bhow\s+(are|r|arte|ar)\s+(you|u|ya|ye|doing)\b|"
        r"\bhow'?re\s+(you|u|ya|things)\b|"
        r"\bhow.?s\s+(it\s+going|things|life|going)\b|"
        r"\bhow\s+do\s+you\s+do\b",
        re.I),
     f"I'm good, {config.USER_NAME}. Ready when you are."),

    # "what are you doing / what are you up to"
    (re.compile(
        r"\bwhat\s+are\s+you\s+(doing|up\s+to)\b|"
        r"\bwhat.?s\s+up\b",
        re.I),
     "I'm here with you, listening and ready to help."),

    # "I am tired / I'm tired / I'm exhausted" — fixed to match "I am" not just "I'm"
    (re.compile(r"\b(i'?m|i\s+am)\s+(tired|exhausted|sleepy|drained)\b", re.I),
     f"Yeah, I get that. Want me to put on something relaxing, or help you ease back into work?"),

    (re.compile(r"\b(i'?m|i\s+am)\s+(stressed|overwhelmed|frustrated|burnt?\s*out)\b", re.I),
     "Take a breath. What do you need right now?"),

    (re.compile(r"\b(i'?m|i\s+am)\s+(bored|so\s+bored)\b", re.I),
     "Want me to find something to watch, or put on some music?"),

    (re.compile(r"\b(i'?m|i\s+am)\s+(happy|great|good|doing\s+well|feeling\s+good)\b", re.I),
     f"Good to hear! What can I do for you?"),

    (re.compile(r"\bi\s+need\s+help\b|\bcan\s+you\s+help\s+me\b", re.I),
     "Of course — what do you need?"),

    (re.compile(r"\b(good\s+morning|morning\s+maki)\b", re.I),
     f"Morning, {config.USER_NAME}! What are we doing today?"),

    (re.compile(r"\b(good\s+(evening|afternoon))\b", re.I),
     f"Hey, {config.USER_NAME}. What do you need?"),

    (re.compile(r"\b(good\s+night|goodnight)\b", re.I),
     "Good night! Want me to put the PC to sleep?"),

    (re.compile(r"\b(thank\s+you|thanks|cheers|thx|ty)\b", re.I),
     "Anytime!"),

    # Bare greeting (whole message is just a greeting)
    (re.compile(r"^(hi+|hello+|hey+|sup|yo|wassup)\.?\s*$", re.I),
     "Hey! What do you need?"),

    # "no that's fine / that is fine / that's okay"
    (re.compile(
        r"\b(no[,\s]+that.?s?\s+(fine|okay|alright|good|all)|"
        r"no[,\s]+that\s+is\s+(fine|okay|good|alright)|"
        r"that.?s?\s+(fine|okay|all\s+good|alright)|"
        r"that\s+is\s+(fine|okay|good|alright))\b",
        re.I),
     "Alright, no problem."),

    # Compliment
    (re.compile(
        r"\b(you.?re?\s+(great|awesome|the\s+best|amazing|good)|well\s+done|nice\s+work)\b",
        re.I),
     "Appreciate it! What else can I do for you?"),
]


# ── Timing helpers ────────────────────────────────────────────────────────────

def _finish(t0: float) -> None:
    global _last_ms
    _last_ms = int((time.monotonic() - t0) * 1000)
    logger.info("Processing time: %d ms", _last_ms)


# ── Main entry point ──────────────────────────────────────────────────────────

# ════════════════════════════════════════════════════════════════════════════
# V18 — Voice meta-commands (think mode, stop, etc.)
# ════════════════════════════════════════════════════════════════════════════

# Sticky think-mode triggers — stay ON until user disables
_THINK_ON_STICKY_RE = re.compile(
    r"^(?:please\s+|can\s+you\s+)?"
    r"(?:turn\s+on\s+|enable\s+|activate\s+|start\s+)?"
    r"(?:keep\s+thinking|stay\s+smart|smart\s+mode\s+on|"
    r"think\s+mode\s+on|deep\s+thinking\s+on|"
    r"keep\s+(?:being\s+)?smart|reason\s+about\s+everything|"
    r"think\s+carefully\s+(?:about\s+)?everything)\s*\.?$",
    re.I,
)
_THINK_OFF_STICKY_RE = re.compile(
    r"^(?:please\s+|can\s+you\s+)?"
    r"(?:stop\s+thinking|go\s+fast|smart\s+mode\s+off|"
    r"think\s+mode\s+off|fast\s+mode|just\s+be\s+fast|"
    r"normal\s+mode|don'?t\s+(?:think|reason)\s+(?:so\s+)?(?:hard|much))\s*\.?$",
    re.I,
)
# One-shot think trigger — single utterance, then back to normal
_THINK_ONESHOT_RE = re.compile(
    r"^(?:please\s+|hey\s+maki\s+|)?"
    r"(?:think\s+about\s+(?:it|this|that)|"
    r"think\s+(?:harder|deeply|carefully|first)|"
    r"be\s+smart\s+about\s+(?:it|this)|"
    r"reason\s+about\s+(?:it|this|that)|"
    r"use\s+your\s+brain|think\s+twice)\s*\.?$",
    re.I,
)
# Stop / shut-up commands
_STOP_RE = re.compile(
    r"^(?:please\s+|maki\s+|)?"
    r"(?:stop|shut\s+up|be\s+quiet|quiet|silence|cancel|"
    r"nevermind|never\s+mind|forget\s+(?:it|that)|"
    r"pause|abort|stop\s+talking|stop\s+it)\s*\.?$",
    re.I,
)


def _handle_voice_meta(text: str) -> str | None:
    """V18: handle meta-commands ('think', 'stop', etc.) before routing.
    Returns the spoken reply, or None to let the rest of the pipeline run."""
    t = (text or "").lower().strip().rstrip(".,!?")

    # ── Stop commands ────────────────────────────────────────────────────
    if _STOP_RE.match(t):
        memory.request_stop()
        return "Okay, stopping."

    # ── Sticky think-mode ON ─────────────────────────────────────────────
    if _THINK_ON_STICKY_RE.match(t):
        memory.set_think_mode(True)
        # Update GUI toggle if available
        try:
            import main as _m
            if hasattr(_m, "window") and _m.window:
                _m.window.set_think(True)
        except Exception: pass
        return "Think mode on. I'll reason carefully about everything from now on."

    # ── Sticky think-mode OFF ────────────────────────────────────────────
    if _THINK_OFF_STICKY_RE.match(t):
        memory.set_think_mode(False)
        try:
            import main as _m
            if hasattr(_m, "window") and _m.window:
                _m.window.set_think(False)
        except Exception: pass
        return "Fast mode on. Snappy responses."

    # ── One-shot think (next turn only) ──────────────────────────────────
    # Note: we DON'T flip memory here — instead we let the user know we
    # need a follow-up command. The one-shot pattern is rarely used alone;
    # usually it's "think about [topic]" — handled by the agent path.
    # We just acknowledge.
    # (If user says "think about [X]", that's a real request with content,
    # caught by the regex below.)

    return None


def process(raw_text: str) -> str:
    """
    Full V4 pipeline. Returns a spoken reply string.
    """
    if not raw_text or not raw_text.strip():
        return ""

    t0 = time.monotonic()

    # Step 1: Clean
    text = clean_transcript(raw_text)
    if not text:
        text = raw_text.strip()
    logger.info("V4 processing: %r", text)

    # Step 1.5: V18 — Voice control commands (think / stop / mode toggles).
    # These intercept BEFORE any routing because they're meta-commands about
    # how Maki should behave, not actions to take.
    _meta = _handle_voice_meta(text)
    if _meta is not None:
        memory.add("user", text); memory.add("assistant", _meta)
        _finish(t0)
        return _meta

    # Step 2a: Pending CONFIRMATION (risky action waiting for yes/no)
    if has_confirm():
        confirm_dec = pop_confirm()
        if _AFFIRMATIVE.match(text):
            memory.add("user", text)
            reply = _run_tool(confirm_dec)
            reply = reply or "Done."
            memory.add("assistant", reply)
            memory.set_last_action(confirm_dec)
            _finish(t0)
            return reply
        elif _NEGATIVE.match(text):
            memory.add("user", text)
            reply = "Cancelled. Let me know if you need anything else."
            memory.add("assistant", reply)
            _finish(t0)
            return reply
        else:
            set_confirm(None)   # not a yes/no — treat as new command

    # Step 2b: Pending CLARIFICATION
    if has_pending():
        pending = pop_pending()
        memory.add("user", text)
        # V8: if the user just confirms ("yes"/"yeah sure") and the pending
        # decision already carries a usable target, keep it — don't overwrite
        # the target with the word "yes".
        if (_AFFIRM_BROAD.match(text) and pending.get("target")
                and pending.get("action") not in ("none", "", None)):
            pass  # keep pending["target"] as-is
        elif _NEGATIVE.match(text):
            reply = "Okay, cancelled. What else can I do?"
            memory.add("assistant", reply)
            _finish(t0)
            return reply
        else:
            pending["target"] = text
        reply = _run_tool(pending)
        if not reply:
            reply = _chat_or_fallback(text)
        memory.add("assistant", reply)
        memory.set_last_action(pending)
        _finish(t0)
        return reply

    # Step 2c: Pending WEB SEARCH confirmation ("want me to search?" → "yeah sure")
    if memory.has_pending_web_search():
        # V9: also accept explicit "look it up" style confirmations, not just yes/sure
        _confirms_search = bool(_AFFIRM_BROAD.match(text)) or bool(re.search(
            r"\b(look\s+(?:it|that)\s+up|look\s+up|search\s+(?:it|that|for\s+it)|"
            r"go\s+ahead|do\s+(?:it|that)|please\s+do|yes\s+please|find\s+(?:it|out))\b",
            text.lower(),
        ))
        if _confirms_search:
            q = memory.pop_pending_web_search()
            memory.add("user", text)
            dec   = _decision(intent="safe_action", action="search_web", target=q)
            reply = _run_tool(dec)
            memory.set_last_action(dec)
            memory.add("assistant", reply)
            _finish(t0)
            return reply
        elif _NEGATIVE.match(text):
            memory.pop_pending_web_search()
            memory.add("user", text)
            reply = "Okay, I'll leave it."
            memory.add("assistant", reply)
            _finish(t0)
            return reply
        else:
            memory.pop_pending_web_search()   # not a yes/no — treat as new command

    # Step 2d: Correction / complaint ("you didn't open it", "answer yourself")
    correction_reply = _handle_correction(text)
    if correction_reply is not None:
        logger.info("Routed to: correction handler")
        memory.add("user", text)
        memory.add("assistant", correction_reply)
        _finish(t0)
        return correction_reply

    # Step 3: Safety
    if _is_risky_transcript(text):
        reply = (
            "That sounds risky — I won't do that automatically. "
            "What exactly do you need? Be specific."
        )
        memory.add("user", text)
        memory.add("assistant", reply)
        _finish(t0)
        return reply

    # Step 3.2: V18 — THINK MODE. When user toggled "Think" ON (GUI button or
    # voice "think"/"be smart"), perception runs FIRST on every turn for deeper
    # reasoning. Default OFF = current fast behavior preserved.
    if memory.is_think_mode():
        try:
            import perception as _pc
            p = _pc.perceive(text)
            if p and not p.is_garbage:
                # Re-route using the corrected/expanded text
                corrected = p.corrected_text.strip()
                if corrected and corrected.lower() != text.lower().strip():
                    logger.info("V18 think-mode: %r → %r (conf %.2f)",
                                text, corrected, p.confidence)
                    text = corrected
                # If confidence is borderline and we have a clarify question, ASK
                elif p.confidence < 0.50 and p.clarify:
                    memory.add("user", text); memory.add("assistant", p.clarify)
                    _pc.ctx.update(user=text, assistant=p.clarify,
                                   action="think-clarify")
                    _finish(t0)
                    return p.clarify
        except Exception as _e:
            logger.debug("V18 think-mode skip: %s", _e)

    # ════════════════════════════════════════════════════════════════════════
    # Step 3.22: V20 — TIER-1 fast-path → TIER-2 Cerebras planner.
    # Replaces the BGE intent router as the primary decision-maker.
    # ────────────────────────────────────────────────────────────────────────
    # Tier 1: a STRICT regex allowlist of ~15 truly unambiguous commands
    #         ("scroll down", "open chrome", "go back", ...). Conf ≥ 0.98.
    #         Zero AI cost on these.
    # Tier 2: everything else goes to Cerebras gpt-oss-120b which reasons
    #         about intent + context and returns an action JSON. The
    #         executor dispatches to existing tool implementations.
    # If Tier 2 returns None (Cerebras down, 429, unparseable) we FALL
    # THROUGH to the legacy steps (multi-action, BGE router, perception,
    # screen-control fast path, compound, basic_classify, agentic brain).
    # Legacy is the safety net — only fires when Tier 2 can't decide.
    # ════════════════════════════════════════════════════════════════════════
    try:
        import tier1_fastpath, cerebras_planner, plan_executor, runtime_context

        v20_plan = None
        v20_source = None

        # — TIER 1 —
        fp = tier1_fastpath.is_trivial(text)
        if fp:
            intent_name, fp_conf, fp_target = fp
            v20_plan = tier1_fastpath.to_plan(intent_name, fp_conf, fp_target)
            v20_source = "tier1"

        # — TIER 2 —
        if v20_plan is None:
            try:
                active = runtime_context.foreground_window_title()
                screen = runtime_context.get_screen_context()
                hist   = memory.get_history()[-3:]
            except Exception:
                active, screen, hist = "", "", []
            v20_plan = cerebras_planner.plan(text,
                                              screen_context=screen,
                                              recent_history=hist,
                                              active_app=active)
            if v20_plan is not None:
                v20_source = "tier2_cerebras"

        # V20 Step 4b: confidence floor 0.75 (was 0.55).
        # Cerebras returns 0.96+ on clear cases. Anything under 0.75 is
        # ambiguous enough that we'd rather fall through to the legacy
        # safety net than execute a low-confidence plan.
        if v20_plan is not None and v20_plan.confidence >= 0.75:
            logger.info("V20 %s: %r -> %s target=%r conf=%.2f",
                        v20_source, text[:60],
                        v20_plan.action, v20_plan.target[:60],
                        v20_plan.confidence)
            v20_reply = plan_executor.execute_plan(v20_plan, original_text=text)
            if v20_reply:
                # Remember last-action so correction handler ("you didn't do
                # it") still works. Use a minimal decision-shaped dict.
                try:
                    memory.set_last_action({
                        "intent":   v20_plan.intent,
                        "action":   v20_plan.action.lower(),
                        "target":   v20_plan.target,
                        "via":      v20_source,
                    })
                except Exception:
                    pass
                memory.add("user", text)
                memory.add("assistant", v20_reply)
                try:
                    import perception as _pc
                    _pc.ctx.update(user=text, assistant=v20_reply,
                                   action=f"v20_{v20_plan.action.lower()}")
                except Exception: pass
                logger.info("Routed to: V20 (%s, %s)", v20_source, v20_plan.action)
                _finish(t0)
                return v20_reply
            # If executor returned empty, let legacy chain try.
            logger.info("V20 %s returned empty for %r — falling to legacy", v20_source, text[:60])
        elif v20_plan is not None:
            logger.info("V20 plan conf %.2f below 0.75 threshold for %r — falling to legacy",
                        v20_plan.confidence, text[:60])
    except Exception as _e:
        logger.warning("V20 tier routing skipped (%s) — using legacy chain", _e)

    # Step 3.25: V17 — Multi-action splitter. "open chrome and search wikipedia"
    # → split on " and " between two clear command clauses, route each.
    # Done BEFORE intent router so we don't lose the second clause.
    multi_reply = _try_multi_action(text)
    if multi_reply is not None:
        logger.info("Routed to: multi-action")
        memory.add("user", text); memory.add("assistant", multi_reply)
        try:
            import perception as _pc
            _pc.ctx.update(user=text, assistant=multi_reply, action="multi_action")
        except Exception: pass
        _finish(t0)
        return multi_reply

    # Step 3.3: V15 BGE intent router — V20 DISABLED.
    # The 30-intent embedding similarity router was the source of the
    # "minimize → maximize" / "click on X → wrong click" / "thank you →
    # alt+left" misroutes the user reported. Tier 1 (above) now handles
    # the truly unambiguous intents with regex (~15 commands at conf 0.98+).
    # Everything else MUST go through Cerebras for real reasoning.
    # Leaving the import and singleton in place so lane_classifier and
    # tests that call _intent_router.classify() keep working — only the
    # production decision path is severed.
    # (If Tier 2 returned None and we got here, the agentic brain at the
    # bottom handles it with V19 lane router — not BGE.)
    pass

    # Step 3.35: V16 — PERCEPTION LAYER. Intent router missed → maybe the
    # transcript is mangled. Run a context-aware LLM correction, then RETRY
    # the intent router with the corrected text. This is what makes Maki
    # actually understand mishearings like "school of chrome" → "scroll
    # the chrome", "wikipedia" (after searching) → "click the wikipedia link".
    try:
        import perception as _pc
        p = _pc.perceive(text)
        if p and not p.is_garbage:
            # Low confidence → ask for clarification instead of guessing
            if p.confidence < 0.55 and p.clarify:
                logger.info("Routed to: perception → clarify (conf %.2f)", p.confidence)
                memory.add("user", text); memory.add("assistant", p.clarify)
                _pc.ctx.update(user=text, assistant=p.clarify, action="clarify")
                _finish(t0)
                return p.clarify
            # V20 DISABLED: was a "perception cleaned the text → re-run BGE
            # intent router on corrected text" path. Since BGE is no longer
            # part of the production decision chain (see step 3.3 note above),
            # we just promote the corrected text to `text` and let Tier 2 /
            # downstream legacy chain handle it.
            corrected = p.corrected_text.strip()
            if corrected and corrected.lower() != text.lower().strip():
                text = corrected
        elif p and p.is_garbage:
            # V19 BUG-5b FIX: previously this hard-returned "Sorry I missed
            # that" — but Groq Whisper transcribed valid English ("oh my god",
            # "bare minimum as it should") that perception flagged as garbage
            # too aggressively. New behavior: ONLY reject if the transcript
            # is genuinely empty or sub-3-chars. Otherwise fall through to
            # the chat lane and let groq_8b / cerebras handle it as a
            # normal conversational turn.
            transcript_chars = len((text or "").strip())
            if transcript_chars < 3:
                ask = p.clarify or "Sorry, I didn't catch that — could you say it again?"
                logger.info("Routed to: perception → garbage (transcript too short: %d chars)",
                            transcript_chars)
                memory.add("user", text); memory.add("assistant", ask)
                _pc.ctx.update(user=text, assistant=ask, action="garbage")
                _finish(t0)
                return ask
            # Valid-looking transcript flagged garbage: let chat lane handle it.
            logger.info("Routed to: perception garbage overridden — falling through to chat (%r)",
                        text[:60])
            _pc.ctx.update(user=text, assistant="", action="garbage_overridden")
            # NO return — fall through; downstream chat lanes will handle.
    except Exception as _e:
        logger.debug("perception skip: %s", _e)

    # Step 3.4: V14 — Screen-control fast-paths (scroll/new-tab/back/type/etc.).
    # These should NEVER bounce to the slow agentic brain.
    sc_reply = _screen_control_fast_path(text)
    if sc_reply is not None:
        logger.info("Routed to: fast_path → screen_control")
        memory.add("user", text)
        memory.add("assistant", sc_reply)
        try:
            import perception as _pc
            _pc.ctx.update(user=text, assistant=sc_reply, action="screen_control")
        except Exception: pass
        _finish(t0)
        return sc_reply

    # Step 3.5: Compound multi-action commands ("open discord and maximize it")
    compound_reply = _handle_compound(text)
    if compound_reply is not None:
        logger.info("Routed to: compound parser")
        memory.add("user", text)
        memory.add("assistant", compound_reply)
        _finish(t0)
        return compound_reply

    # Step 4: Fast-path reflexes — instant, deterministic, zero AI cost.
    # ONLY confident, deterministic tool actions / acks / clarifications take
    # this path. Anything conversational or uncertain → the agentic brain.
    decision = _basic_classify(text)
    if decision is not None:
        intent     = decision.get("intent", "unknown")
        confidence = decision.get("confidence", 0.5)

        # Acknowledgement → instant
        if intent == "acknowledgement":
            import random
            reply = random.choice(["Got it.", "Alright.", "Sure.", "Okay.", "Of course."])
            memory.add("user", text); memory.add("assistant", reply)
            _finish(t0)
            return reply

        # Clarification → set pending, ask
        if intent == "clarification" or decision.get("needs_clarification"):
            q = (decision.get("clarification_question")
                 or "Could you be a bit more specific?")
            if decision.get("action") and decision["action"] != "none":
                set_pending(decision)
            memory.add("user", text); memory.add("assistant", q)
            _finish(t0)
            return q

        # Risky action → confirm
        if intent == "risky_action" or decision.get("requires_confirmation"):
            set_confirm(decision)
            reply = _confirm_question(decision)
            memory.add("user", text); memory.add("assistant", reply)
            _finish(t0)
            return reply

        # Deterministic safe action → run the tool directly (fast, no AI cost)
        if intent in ("safe_action", "current_info") and confidence >= 0.70:
            reply = _run_tool(decision)
            if reply:
                logger.info("Routed to: fast_path → %s", decision.get("action", "?"))
                memory.add("user", text); memory.add("assistant", reply)
                memory.set_last_action(decision)
                _finish(t0)
                return reply

        # Fast-path conversation with a prebuilt response (e.g. "what's my name")
        if intent == "conversation":
            spoken = decision.get("spoken_response", "").strip()
            if spoken:
                memory.add("user", text); memory.add("assistant", spoken)
                _finish(t0)
                return spoken
        # else: fall through to the agentic brain

    # Step 5: THE AGENTIC BRAIN — the LLM thinks, calls tools as needed, and
    # replies naturally. This is where Maki stops routing and starts assisting.
    memory.add("user", text)
    logger.info("Routed to: agentic brain")
    reply = ""
    if agent is not None:
        try:
            reply = agent.respond(text)
        except Exception as e:
            logger.warning("Agentic brain error: %s", e)
            reply = ""
    if not reply:
        # agent.py missing or hard failure — last-resort, still conversational
        reply = _chat_or_fallback(text)
    memory.add("assistant", reply)
    logger.info("Response generated: %r", (reply or "")[:120])
    _finish(t0)
    return reply


def _chat_or_fallback(text: str) -> str:
    """
    V10: route conversation through the agentic brain (the LLM thinks).
    Only used now by the pending-clarification path and as a last resort if
    agent.py is missing. NEVER dumps an internal status message as a reply.
    """
    # Prefer the real agentic brain
    if agent is not None:
        try:
            reply = agent.respond(text)
            if reply:
                return reply
        except Exception as e:
            logger.warning("_chat_or_fallback agent error: %s", e)

    # agent.py unavailable — graceful, conversational, NEVER a status dump
    t_low = text.lower().strip()
    if re.match(r"^(ok|okay|sure|got it|alright|fine|cool|yep|yup|right)\.?\s*$", t_low):
        return "Got it."
    if re.match(r"^(hi+|hey+|hello+|yo|sup|good)\s*[.!]?\s*$", t_low):
        return f"Hey, {config.USER_NAME}! What do you need?"
    if "?" in text or re.match(r"^(what|why|who|when|where|how|is|are|can|do)\b", t_low):
        return ("My reasoning model is briefly unreachable — I can still handle time, "
                "apps, weather, screenshots and more. Want me to try one, or ask again?")
    return "I'm here — what would you like to do?"
