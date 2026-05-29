"""
tier1_fastpath.py — V20 Tier 1 fast-path allowlist.

The ONLY phrases that bypass the Cerebras planner. Strict allowlist of
intents that are 100% unambiguous AND have a high-confidence pattern.

This is what the BGE intent_router (30 intents) becomes — demoted from
"primary decision-maker" to "fast-path filter for the simplest commands".

Anything not on this list goes to cerebras_planner.plan() for real
reasoning.

Returns (intent_name, confidence, handler_callable) or None.
"""

from __future__ import annotations
import logging, re

logger = logging.getLogger(__name__)

# ── Strict allowlist of fast-path intents ────────────────────────────────────
# Each entry: (intent_name, [exact phrase patterns], confidence_floor)
# We use literal-phrase matching with light flexibility (optional "please",
# "now", articles). NO embedding similarity — that's what got us into the
# "minimize → maximize" mess in the first place.

_FASTPATH_INTENTS = (
    ("scroll_down",   [r"^scroll(?:\s+(?:down|page\s+down))?(?:\s+please)?$",
                       r"^page\s+down$"],                                  1.00),
    ("scroll_up",     [r"^scroll\s+up(?:\s+please)?$",
                       r"^page\s+up$"],                                    1.00),
    ("scroll_left",   [r"^scroll\s+left$"],                                1.00),
    ("scroll_right",  [r"^scroll\s+right$"],                               1.00),
    ("browser_back",  [r"^(?:go\s+)?back(?:\s+please)?$"],                 1.00),
    ("browser_forward",[r"^(?:go\s+)?forward(?:\s+please)?$"],             1.00),
    ("browser_refresh",[r"^(?:refresh|reload)(?:\s+(?:the\s+)?(?:page|tab))?$"], 1.00),
    ("new_tab",       [r"^(?:open\s+(?:a\s+)?)?new\s+tab(?:\s+please)?$"], 1.00),
    ("close_tab",     [r"^close\s+(?:this\s+|the\s+)?tab(?:\s+please)?$"], 1.00),
    ("press_enter",   [r"^(?:press|hit|tap)\s+enter$",
                       r"^enter$"],                                        1.00),
    ("press_escape",  [r"^(?:press|hit|tap)\s+(?:esc|escape)$",
                       r"^escape$"],                                       1.00),
    ("screenshot",    [r"^(?:take|grab|capture)\s+(?:a\s+)?screenshot$",
                       r"^screenshot$"],                                   1.00),
    # Open/close/focus a specific app — only when the phrasing is exactly
    # "<verb> <app>" with one of the known apps. Anything more elaborate
    # ("open chrome and search youtube") goes through Cerebras for proper
    # multi-step handling.
    ("open_app",      [r"^(?:open|launch|start)\s+([a-z][a-z0-9 \-]{1,30})$"], 0.98),
    ("close_app",     [r"^(?:close|quit|exit)\s+([a-z][a-z0-9 \-]{1,30})$"],   0.98),
    ("focus_app",     [r"^(?:focus|switch\s+to|go\s+to|bring\s+up)\s+([a-z][a-z0-9 \-]{1,30})$"], 0.98),
)


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace + strip trailing punctuation."""
    t = (text or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    t = t.strip(".,!?")
    return t


def is_trivial(text: str) -> tuple[str, float, str] | None:
    """
    Return (intent_name, confidence, captured_target) if the utterance is
    a trivial fast-path command, else None.

    The third tuple element is the regex capture group if the pattern
    captured one (e.g. the app name in "open chrome"), or "" otherwise.
    """
    t = _normalize(text)
    if not t:
        return None

    for intent, patterns, conf in _FASTPATH_INTENTS:
        for pat in patterns:
            m = re.match(pat, t)
            if m:
                target = m.group(1).strip() if m.groups() else ""
                # Reject compound commands ("open chrome and search youtube")
                # — those need Cerebras to plan a sequence, not a fast-path.
                if " and " in target or " then " in target:
                    logger.info("tier1_fastpath: %r looks compound (target=%r) "
                                "— deferring to Cerebras", text[:60], target)
                    return None
                # Strip leading connectives that the regex may have captured
                # ("focus on spotify" → target="on spotify" → "spotify").
                target = re.sub(r"^(?:on|to|the)\s+", "", target).strip()
                logger.info("tier1_fastpath: %r -> %s (conf=%.2f, target=%r)",
                            text[:60], intent, conf, target)
                return intent, conf, target

    return None


# ── Public helpers for callers ──────────────────────────────────────────────
def available_intents() -> list[str]:
    return [i for (i, _p, _c) in _FASTPATH_INTENTS]


# Map tier-1 intent names to (action, target, params) so they can be
# dispatched through the same plan_executor pipeline as Cerebras plans.
_INTENT_TO_ACTION = {
    "scroll_down":     ("SCROLL", "down",     {"times": 5}),
    "scroll_up":       ("SCROLL", "up",       {"times": 5}),
    "scroll_left":     ("SCROLL", "left",     {"times": 5}),
    "scroll_right":    ("SCROLL", "right",    {"times": 5}),
    "browser_back":    ("KEY",    "alt+left", {}),
    "browser_forward": ("KEY",    "alt+right",{}),
    "browser_refresh": ("KEY",    "f5",       {}),
    "new_tab":         ("KEY",    "ctrl+t",   {}),
    "close_tab":       ("KEY",    "ctrl+w",   {}),
    "press_enter":     ("KEY",    "enter",    {}),
    "press_escape":    ("KEY",    "escape",   {}),
    "screenshot":      ("SCREENSHOT", "",     {}),
    "open_app":        ("OPEN",   None,       {}),   # target = captured app
    "close_app":       ("CLOSE",  None,       {}),
    "focus_app":       ("FOCUS",  None,       {}),
}


def to_plan(intent: str, conf: float, target: str):
    """Convert a tier-1 result to a cerebras_planner.Plan so the executor
    can handle it uniformly."""
    try:
        from cerebras_planner import Plan
    except Exception:
        return None
    if intent not in _INTENT_TO_ACTION:
        return None
    action, fixed_target, params = _INTENT_TO_ACTION[intent]
    final_target = target if fixed_target is None else fixed_target
    return Plan(
        intent     = intent,
        action     = action,
        target     = final_target or "",
        params     = dict(params),
        confidence = conf,
        reasoning  = f"tier1_fastpath:{intent}",
    )
