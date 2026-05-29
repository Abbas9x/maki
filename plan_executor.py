"""
plan_executor.py — V20 Tier 2 Step 3

Takes a Plan (from cerebras_planner) and dispatches it to the right
existing tool implementation. No new tool code — only routing.

Action mapping:
  CLICK       -> screen_control.click_text
  TYPE        -> screen_control.type_text
  KEY         -> screen_control.press_keys (multi-step: "ctrl+a then ctrl+c")
  SCROLL      -> screen_control.scroll
  OPEN        -> agent.open_app
  CLOSE       -> agent.close_app
  FOCUS       -> window_tools.focus_window
  SCREENSHOT  -> vision_tools.capture (cached)
  VISION      -> vision_tools.look_at_screen(plan.target)
  SEARCH      -> actions.search_google / search_youtube
  CHAT        -> V19 lane router (with Think-keyword override)
  MEMORY      -> memory.add / search_history
"""

from __future__ import annotations
import logging, re, time

logger = logging.getLogger(__name__)


# ── Think-keyword override (V20 Step 3 requirement) ─────────────────────────
# If Cerebras decides action=CHAT and the user's words include any of these
# coding/thinking signals, we route to github_premium (GPT-4o) directly —
# regardless of whether the 🧠 Think toggle is on. The user explicitly asked
# for thinking/code.
_THINK_KEYWORDS = re.compile(
    r"\b("
    r"think(?:\s+(?:and|about|through))?"
    r"|code"
    r"|write\s+(?:me\s+)?(?:a\s+)?(?:script|program|function|class|method)"
    r"|program"
    r"|algorithm"
    r"|implement"
    r"|debug"
    r"|refactor"
    r"|design\s+(?:a\s+)?(?:database|schema|api|class|system)"
    r"|architect"
    r"|solve\s+this"
    r"|reason\s+(?:about|through)"
    r"|prove\s+that"
    r")\b",
    re.I,
)


def _wants_think_lane(original_text: str) -> bool:
    """User wants Think-mode (premium reasoning) even if the toggle is off."""
    return bool(_THINK_KEYWORDS.search(original_text or ""))


# ── Per-action handlers ─────────────────────────────────────────────────────
def _do_click(target: str) -> str:
    try:
        import screen_control
        return screen_control.click_text(target)
    except Exception as e:
        return f"Click failed: {e}"


def _do_type(target: str) -> str:
    try:
        import screen_control
        return screen_control.type_text(target)
    except Exception as e:
        return f"Type failed: {e}"


def _do_key(target: str) -> str:
    """Supports single combo ('ctrl+c') or sequences ('ctrl+a then ctrl+c')."""
    try:
        import screen_control
        steps = [s.strip() for s in re.split(r"\s*(?:then|,|;)\s*", target) if s.strip()]
        if not steps:
            return "No key combo to press."
        results = []
        for combo in steps:
            r = screen_control.press_keys(combo)
            results.append(r or f"Pressed {combo}")
            time.sleep(0.1)   # small pause between key sequences
        return " then ".join(results)
    except Exception as e:
        return f"Key press failed: {e}"


def _do_scroll(target: str, params: dict) -> str:
    direction = (target or "down").strip().lower() or "down"
    times = int(params.get("times", params.get("amount", 5)))
    try:
        import screen_control
        return screen_control.scroll(direction=direction, amount=times)
    except Exception as e:
        return f"Scroll failed: {e}"


def _do_open(target: str) -> str:
    try:
        import agent
        return agent.open_app(target)
    except Exception as e:
        return f"Open failed: {e}"


def _do_close(target: str) -> str:
    try:
        import agent
        return agent.close_app(target)
    except Exception as e:
        return f"Close failed: {e}"


def _do_focus(target: str) -> str:
    try:
        import window_tools
        res = window_tools.focus_window(target)
        if isinstance(res, dict):
            return res.get("message") or res.get("status") or f"Focused {target}."
        return str(res)
    except Exception as e:
        return f"Focus failed: {e}"


def _do_screenshot() -> str:
    try:
        import vision_tools
        b64 = vision_tools._capture_b64()
        return "Screenshot taken." if b64 else "Couldn't capture the screen."
    except Exception as e:
        return f"Screenshot failed: {e}"


def _do_vision(target: str) -> str:
    """Ask qwen3-vl:4b about what's on screen."""
    try:
        import vision_tools
        question = target or "What's on the screen right now? Describe it briefly."
        return vision_tools.look_at_screen(question)
    except Exception as e:
        return f"Vision failed: {e}"


def _do_search(target: str, params: dict) -> str:
    engine = (params.get("engine") or "google").lower()
    try:
        import actions
        if engine == "youtube":
            return actions.search_youtube(target)
        return actions.search_google(target)
    except Exception as e:
        return f"Search failed: {e}"


def _do_chat(original_text: str, plan_intent: str) -> str:
    """
    Route to the V19 lane router for a conversational reply.

    THINK-KEYWORD OVERRIDE: when the user said something like 'think and code
    a for loop' or 'write me a script', we force the github_premium lane
    even if the 🧠 Think toggle is off. The planner correctly classified
    this as CHAT — but the right CHAT lane is the premium reasoning one,
    not the fast 8B social one.
    """
    try:
        import memory, lane_classifier, lane_dispatch, brain as _brain

        think_mode_on = False
        try: think_mode_on = memory.is_think_mode()
        except Exception: pass

        # FORCE Think lane on coding/thinking utterances regardless of toggle
        forced_think = _wants_think_lane(original_text)
        if forced_think and not think_mode_on:
            logger.info("plan_executor: forcing github_premium for think/code "
                        "keyword in %r", original_text[:60])
            think_mode_on = True

        router = getattr(_brain, "_intent_router", None)
        lane, info = lane_classifier.select_lane(
            original_text, think_mode_on=think_mode_on, router=router,
        )
        # If we forced Think mode, override the classifier choice too
        if forced_think:
            lane = "github_premium"
            info["reason"] = "think_keyword_override"
        lane_classifier.log_decision(info)

        if lane in ("hermes_tools", "vision"):
            # Shouldn't normally happen for CHAT — but be defensive.
            lane = "cerebras_120b"

        import config as _cfg
        _user = getattr(_cfg, "USER_NAME", "friend")
        system = (
            f"You are Maki, {_user}'s personal AI assistant. "
            "Reply in plain English, 1-3 short sentences, voice-friendly. "
            "NEVER emit JSON, function calls, raw tool names, or code blocks. "
            "For genuine questions or chitchat, just answer directly."
        )
        if lane == "github_premium":
            system = (
                f"You are Maki, {_user}'s personal AI assistant. The user "
                "asked for deep reasoning. Give a complete answer: "
                "explanations can be long, code blocks are welcome, "
                "structured output is fine. Be thorough."
            )

        history = []
        try: history = memory.get_history()
        except Exception: pass
        reply, dinfo = lane_dispatch.dispatch(original_text, history, lane,
                                              system=system)
        if reply:
            logger.info("plan_executor: CHAT answered via %s",
                        dinfo.get("lane_used"))
            return reply
        return "I'm here — what would you like me to do?"
    except Exception as e:
        logger.warning("plan_executor: CHAT path failed: %s", e)
        return "I'm here — what would you like me to do?"


def _do_memory(target: str, params: dict, original_text: str) -> str:
    """Light-weight memory action — store a fact or recall by keyword."""
    try:
        import memory
        op = (params.get("op") or "").lower()
        if op == "recall" or original_text.lower().startswith(("what did i", "do you remember", "what was")):
            hits = memory.search_history(target, limit=3)
            if not hits:
                return f"I don't have anything about \"{target}\" in memory."
            return " | ".join(h.get("content","")[:120] for h in hits)
        # default: store
        memory.add("user", original_text)
        memory.add("assistant", f"Got it — I'll remember: {target or original_text}")
        return f"Got it — I'll remember that."
    except Exception as e:
        return f"Memory action failed: {e}"


# ── Main dispatcher ─────────────────────────────────────────────────────────
def execute_plan(plan, original_text: str = "") -> str:
    """
    Dispatch a Plan to the matching tool. `original_text` is the user's
    actual utterance (used by CHAT for the Think-keyword override).

    Returns the spoken/displayed reply string.
    """
    if plan is None:
        return ""
    action = plan.action
    target = plan.target or ""
    params = plan.params or {}

    logger.info("plan_executor: %s target=%r params=%s conf=%.2f reason=%r",
                action, target[:80], params, plan.confidence, plan.reasoning[:80])

    if action == "CLICK":      return _do_click(target)
    if action == "TYPE":       return _do_type(target)
    if action == "KEY":        return _do_key(target)
    if action == "SCROLL":     return _do_scroll(target, params)
    if action == "OPEN":       return _do_open(target)
    if action == "CLOSE":      return _do_close(target)
    if action == "FOCUS":      return _do_focus(target)
    if action == "SCREENSHOT": return _do_screenshot()
    if action == "VISION":     return _do_vision(target)
    if action == "SEARCH":     return _do_search(target, params)
    if action == "CHAT":       return _do_chat(original_text or plan.intent, plan.intent)
    if action == "MEMORY":     return _do_memory(target, params, original_text or plan.intent)

    return f"(unknown action {action})"
