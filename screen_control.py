"""
screen_control.py — V13 Maki Screen Control: act on what Maki sees.

Lets Maki actually DO things on the screen — scroll, type, click, hotkeys,
browser navigation — using pyautogui (with pydirectinput fallback for
games / anti-cheat / DirectX surfaces).

Public API (called by agent.py tools):
  scroll(direction, amount)         — scroll up/down/left/right
  type_text(text)                   — type into the focused input
  press_keys(combo)                 — single key or chord ("ctrl+t", "alt+left")
  click_at(x, y, button, double)    — mouse click at absolute coords
  click_text(target_label)          — vision-grounded click (uses qwen2.5vl)
  browser_back / forward / refresh / new_tab / close_tab / switch_tab
  go_to_url(url)
  press_enter() / press_escape() / press_tab()
  get_cursor_position() / get_screen_size()

Safety:
  - pyautogui FAILSAFE remains ON (slam mouse to top-left to abort)
  - All actions log what they did, so you can audit
  - type_text refuses to type into nothing with focus = browser-address-bar guard
"""

from __future__ import annotations
import logging, time
from typing import Optional

logger = logging.getLogger(__name__)


def _invalidate_vision_cache() -> None:
    """V14.5: clear the screenshot cache after any action that changes the screen."""
    try:
        import vision_tools
        vision_tools.invalidate_cache()
    except Exception:
        pass

# ── Lazy / safe imports ─────────────────────────────────────────────────────
try:
    import ctypes
    # Make the process Per-Monitor V2 DPI-aware BEFORE importing pyautogui,
    # so coordinates aren't virtualized on high-DPI / scaled displays.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try: ctypes.windll.user32.SetProcessDPIAware()
        except Exception: pass
except Exception:
    pass

try:
    import pyautogui
    # V19 BUG-4b FIX: FAILSAFE was triggering on normal utterances ("thank
    # you", "let's get to") because brief cursor moves during typing brushed
    # screen corners. The real safety net is safety.is_risky + confirmation
    # gating before any destructive action — not a hair-trigger corner check.
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE    = 0.05   # tiny pause between calls
    _PYAG_OK = True
except Exception as e:
    logger.error("pyautogui unavailable: %s", e)
    pyautogui = None
    _PYAG_OK = False

try:
    import pydirectinput
    pydirectinput.FAILSAFE = False   # V19 BUG-4b: see note above
    pydirectinput.PAUSE    = 0.05
    _PDI_OK = True
except Exception:
    pydirectinput = None
    _PDI_OK = False


# ── Helpers ─────────────────────────────────────────────────────────────────
def _ok_check() -> Optional[str]:
    if not _PYAG_OK:
        return "Mouse/keyboard control isn't available — pyautogui isn't installed."
    return None


def get_screen_size() -> tuple[int, int]:
    if not _PYAG_OK: return (0, 0)
    try: return tuple(pyautogui.size())
    except Exception: return (0, 0)


def get_cursor_position() -> tuple[int, int]:
    if not _PYAG_OK: return (0, 0)
    try: return tuple(pyautogui.position())
    except Exception: return (0, 0)


# ── Scrolling ───────────────────────────────────────────────────────────────
def scroll(direction: str = "down", amount: int = 5) -> str:
    """direction: up|down|left|right. amount: number of separate wheel-ticks
    (V14.4: each tick is its own discrete scroll, with a tiny sleep between
    them so long pages animate smoothly and the user can tell things moved)."""
    if (e := _ok_check()): return e
    import time as _t
    direction = (direction or "down").lower().strip()
    try:
        amount = max(1, min(int(amount), 100))
    except Exception:
        amount = 5
    notch = 120
    try:
        if direction in ("down", "d", "downward", "downwards"):
            for _ in range(amount):
                pyautogui.scroll(-notch); _t.sleep(0.02)
            d_word = "down"
        elif direction in ("up", "u", "upward", "upwards"):
            for _ in range(amount):
                pyautogui.scroll(notch); _t.sleep(0.02)
            d_word = "up"
        elif direction == "right":
            for _ in range(amount):
                pyautogui.hscroll(notch); _t.sleep(0.02)
            d_word = "right"
        elif direction == "left":
            for _ in range(amount):
                pyautogui.hscroll(-notch); _t.sleep(0.02)
            d_word = "left"
        else:
            return f"I don't know how to scroll '{direction}'."
        logger.info("screen_control: scroll %s x%d", d_word, amount)
        _invalidate_vision_cache()
        # V14.4: report the amount so user can hear it worked
        if amount == 1:
            return f"Scrolled {d_word}."
        return f"Scrolled {d_word} {amount} times."
    except Exception as e:
        return f"Scroll failed: {e}"


# ── Typing ──────────────────────────────────────────────────────────────────
def type_text(text: str, speed: float = 0.015) -> str:
    """Type a string into whatever has keyboard focus."""
    if (e := _ok_check()): return e
    if not text:
        return "Nothing to type."
    try:
        pyautogui.typewrite(text, interval=speed)
        logger.info("screen_control: typed %d chars", len(text))
        _invalidate_vision_cache()
        return f"Typed: {text[:60]}{'…' if len(text) > 60 else ''}"
    except Exception as e:
        # pyautogui can't type unicode; fall back to clipboard paste
        try:
            import pyperclip
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")
            return f"Pasted: {text[:60]}{'…' if len(text) > 60 else ''}"
        except Exception:
            return f"Type failed: {e}"


# ── Key combos ──────────────────────────────────────────────────────────────
_KEY_ALIASES = {
    "enter": "enter", "return": "enter", "esc": "escape", "escape": "escape",
    "tab": "tab", "space": "space", "backspace": "backspace", "del": "delete",
    "delete": "delete", "ctrl": "ctrl", "control": "ctrl", "alt": "alt",
    "shift": "shift", "win": "win", "windows": "win", "cmd": "win",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "home": "home", "end": "end", "pageup": "pageup", "pagedown": "pagedown",
}


def press_keys(combo: str) -> str:
    """
    V14.5: Press one or MORE sequential key combos. Examples:
      'enter'                       → press Enter
      'ctrl+t'                      → Ctrl+T (one chord)
      'ctrl+a then backspace'       → Ctrl+A, then Backspace (two presses)
      'ctrl+a and backspace'        → same (two presses)
      'ctrl+a, backspace, enter'    → three presses
    """
    if (e := _ok_check()): return e
    if not combo:
        return "No keys specified."

    # V14.5: split on "then" / ", " / " and " into sequential chords
    import re as _re
    raw_chords = _re.split(r"\s*(?:,| and | then |;)\s*", combo.strip())
    raw_chords = [c.strip() for c in raw_chords if c.strip()]
    if not raw_chords:
        return "No keys specified."

    results = []
    try:
        for chord in raw_chords:
            parts = [p.strip().lower() for p in chord.replace(" ", "").split("+") if p.strip()]
            keys  = [_KEY_ALIASES.get(p, p) for p in parts]
            if not keys: continue
            if len(keys) == 1:
                pyautogui.press(keys[0])
            else:
                pyautogui.hotkey(*keys)
            results.append("+".join(keys))
            time.sleep(0.05)
        logger.info("screen_control: pressed %s", " then ".join(results))
        _invalidate_vision_cache()
        if len(results) == 1:
            return f"Pressed {results[0]}."
        return f"Pressed {' then '.join(results)}."
    except Exception as e:
        return f"Key press failed: {e}"


def press_enter()  -> str: return press_keys("enter")
def press_escape() -> str: return press_keys("escape")
def press_tab()    -> str: return press_keys("tab")


# ── Mouse click ─────────────────────────────────────────────────────────────
def click_at(x: int, y: int, button: str = "left", double: bool = False) -> str:
    if (e := _ok_check()): return e
    try:
        x, y = int(x), int(y)
    except Exception:
        return "Invalid coordinates."
    sw, sh = get_screen_size()
    if not (0 <= x < sw and 0 <= y < sh):
        return f"Coordinates ({x},{y}) are off-screen ({sw}x{sh})."
    btn = (button or "left").lower()
    try:
        if double:
            pyautogui.doubleClick(x, y, button=btn)
        else:
            pyautogui.click(x, y, button=btn)
        logger.info("screen_control: %sclick %s @ (%d,%d)",
                    "double-" if double else "", btn, x, y)
        _invalidate_vision_cache()
        return f"Clicked at ({x},{y})."
    except Exception as e:
        return f"Click failed: {e}"


def move_mouse(x: int, y: int, duration: float = 0.2) -> str:
    if (e := _ok_check()): return e
    try:
        pyautogui.moveTo(int(x), int(y), duration=float(duration))
        return f"Moved cursor to ({x},{y})."
    except Exception as e:
        return f"Move failed: {e}"


# ── Vision-grounded click ───────────────────────────────────────────────────
def click_text(target_label: str) -> str:
    """
    Find a UI element by description and click it. V14 strategy:
      1. Try Windows UIAutomation tree (deterministic, fast, exact names)
      2. Fall back to vision-model grounding (qwen3-vl returns coords)
    """
    if (e := _ok_check()): return e
    if not target_label:
        return "What should I click?"

    # ── 1. UIAutomation first (no vision call needed) ────────────────────
    try:
        import ui_tree
        ui_reply = ui_tree.invoke_element_by_name(target_label)
        if ui_reply and not ui_reply.lower().startswith(("couldn't find",
                "ui tree reader", "tell me which", "no foreground")):
            return ui_reply
    except Exception as e:
        logger.debug("click_text UIA path failed: %s", e)

    # ── 2. Vision fallback ───────────────────────────────────────────────
    try:
        import vision_tools
    except Exception as e:
        return f"Vision tool unavailable: {e}"

    b64 = vision_tools._capture_b64()
    if not b64:
        return "Couldn't capture the screen."
    # V19 BUG-2b FIX: bypass _ask_vision (which prepends the "describe screen"
    # system prompt and causes the model to answer descriptively instead of
    # with coordinates). Call _ollama_vision directly with a coords-only
    # prompt. The model is forced to reply CLICK:x,y or NOT_FOUND, nothing
    # else.
    click_prompt = (
        f"You are a UI click-coordinate engine. Look at this screenshot and "
        f"locate the element described as: '{target_label}'.\n\n"
        f"Reply with EXACTLY one line, no other text, no explanation:\n"
        f"  CLICK:x,y    where x and y are integer pixel coordinates of the\n"
        f"               CENTER of the element on the screenshot\n"
        f"  NOT_FOUND    if you cannot see the element\n\n"
        f"Do NOT describe the screen. Do NOT explain. ONLY output CLICK:x,y "
        f"or NOT_FOUND."
    )
    raw = vision_tools._ollama_vision(click_prompt, b64, timeout=20)
    if not raw or "not_found" in raw.lower():
        return f"I couldn't find '{target_label}' on the screen."
    import re
    # Accept CLICK:x,y (preferred) or bare x,y (fallback for older prompts)
    m = re.search(r"CLICK\s*:\s*(\d{1,5})\s*,\s*(\d{1,5})", raw, re.I)
    if not m:
        m = re.search(r"(\d{1,5})\s*[,x]\s*(\d{1,5})", raw)
    if not m:
        return f"I couldn't find '{target_label}' on the screen."
    x, y = int(m.group(1)), int(m.group(2))

    # Coords are in downscaled-image space — rescale to real screen
    sw, sh = get_screen_size()
    max_side = vision_tools.MAX_DIMENSION
    longest_screen = max(sw, sh)
    scale = longest_screen / max_side if longest_screen > max_side else 1.0
    x_real, y_real = int(x * scale), int(y * scale)
    return click_at(x_real, y_real)


# ── Browser navigation (works in any focused browser tab) ───────────────────
def browser_back()    -> str: return press_keys("alt+left")
def browser_forward() -> str: return press_keys("alt+right")
def browser_refresh() -> str: return press_keys("f5")
def new_tab()         -> str: return press_keys("ctrl+t")
def close_tab()       -> str: return press_keys("ctrl+w")
def switch_tab()      -> str: return press_keys("ctrl+tab")
def reopen_tab()      -> str: return press_keys("ctrl+shift+t")


def go_to_url(url: str) -> str:
    """Focus the address bar (Ctrl+L), type URL, press Enter."""
    if (e := _ok_check()): return e
    if not url:
        return "What URL?"
    try:
        pyautogui.hotkey("ctrl", "l")
        time.sleep(0.15)
        pyautogui.typewrite(url, interval=0.012)
        time.sleep(0.1)
        pyautogui.press("enter")
        logger.info("screen_control: nav to %s", url)
        return f"Going to {url}."
    except Exception as e:
        return f"Navigation failed: {e}"
