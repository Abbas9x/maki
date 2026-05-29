"""
runtime_context.py — V20 Tier 2

Cheap context providers the Cerebras planner needs on every call:
  - foreground_window_title()   — current active app (cheap win32gui call)
  - get_screen_context()        — last vision description (cached up to 30s)
  - set_screen_context()        — vision_tools calls this after each look

Step 5 will hook set_screen_context() into vision_tools.look_at_screen.
For now the cache is just exposed; if nothing has set it, returns "".
"""

from __future__ import annotations
import logging, time

logger = logging.getLogger(__name__)

# ── Active app ───────────────────────────────────────────────────────────────
def foreground_window_title() -> str:
    """Returns the current foreground window title, or '' on failure.
    Cheap (<1 ms on Windows)."""
    try:
        import win32gui
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return ""
        return (win32gui.GetWindowText(hwnd) or "")[:120]
    except Exception:
        return ""


# ── Screen-context cache (30s TTL) ──────────────────────────────────────────
SCREEN_TTL_S = 30.0
_screen_text: str = ""
_screen_ts:   float = 0.0


def set_screen_context(description: str) -> None:
    """Called by vision_tools.look_at_screen() after every successful
    vision call. Stores the description with a fresh timestamp."""
    global _screen_text, _screen_ts
    if description and isinstance(description, str):
        _screen_text = description[:1500]
        _screen_ts   = time.time()


def get_screen_context() -> str:
    """Returns the cached screen description if it's < SCREEN_TTL_S old,
    otherwise empty string. Cerebras planner uses this as `screen_context`."""
    if not _screen_text:
        return ""
    if (time.time() - _screen_ts) > SCREEN_TTL_S:
        return ""
    return _screen_text


def screen_context_age_s() -> float:
    """Seconds since the last vision description was cached."""
    if not _screen_ts:
        return float("inf")
    return time.time() - _screen_ts
