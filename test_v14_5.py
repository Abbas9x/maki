"""V14.5 — the real bug fixes from the 400-line log."""
from __future__ import annotations
import sys, importlib, time
from unittest.mock import patch, MagicMock

try:
    from dotenv import load_dotenv; load_dotenv()
except Exception: pass

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(("  PASS  " if cond else "  FAIL  "), name, "-", str(detail)[:130])

print("=" * 60)
print("V14.5 TEST SUITE")
print("=" * 60)

import brain, agent, screen_control, vision_tools, window_tools, memory
importlib.reload(brain); importlib.reload(agent)
importlib.reload(vision_tools); importlib.reload(screen_control)

# ── 1. Screenshot cache works (biggest win) ──────────────────────────────
print("\n[1] Screenshot cache (saves repeated captures)")
call_count = {"n": 0}
def fake_capture():
    call_count["n"] += 1
    from PIL import Image
    return Image.new("RGB", (1280, 800), "white")
with patch.object(vision_tools, "_capture_full_screen", side_effect=fake_capture):
    vision_tools.invalidate_cache()
    b1 = vision_tools._capture_b64()
    b2 = vision_tools._capture_b64()   # should be cached
    b3 = vision_tools._capture_b64()
    check("cache hits: 3 calls -> 1 real capture", call_count["n"] == 1,
          f"actual captures: {call_count['n']}")
    check("returned b64s are identical", b1 == b2 == b3)

    # Force fresh
    vision_tools.invalidate_cache()
    b4 = vision_tools._capture_b64()
    check("invalidate_cache forces fresh capture", call_count["n"] == 2,
          f"after invalidate: {call_count['n']}")

# ── 2. Multi-key parsing (ctrl+a AND backspace = two presses) ────────────
print("\n[2] Multi-key sequential parsing")
press_log = []
hot_log = []
with patch.object(screen_control.pyautogui, "press", side_effect=lambda k: press_log.append(k)), \
     patch.object(screen_control.pyautogui, "hotkey", side_effect=lambda *k: hot_log.append("+".join(k))):
    press_log.clear(); hot_log.clear()
    r = screen_control.press_keys("ctrl+a and backspace")
    check("'ctrl+a and backspace' -> 2 presses",
          hot_log == ["ctrl+a"] and press_log == ["backspace"],
          f"hot={hot_log} press={press_log}")
    press_log.clear(); hot_log.clear()
    r = screen_control.press_keys("ctrl+a then delete")
    check("'ctrl+a then delete' -> 2 presses",
          hot_log == ["ctrl+a"] and press_log == ["delete"],
          f"hot={hot_log} press={press_log}")
    press_log.clear(); hot_log.clear()
    r = screen_control.press_keys("enter")
    check("single 'enter' still works",
          press_log == ["enter"] and not hot_log, f"hot={hot_log} press={press_log}")

# ── 3. Select-all / common edit shortcuts ────────────────────────────────
print("\n[3] Edit shortcuts (select all, copy, paste, undo, clear)")
press_log.clear(); hot_log.clear()
with patch.object(screen_control.pyautogui, "press", side_effect=lambda k: press_log.append(k)), \
     patch.object(screen_control.pyautogui, "hotkey", side_effect=lambda *k: hot_log.append("+".join(k))):
    for utt, expected_hot in [
        ("select all",                                "ctrl+a"),
        ("select everything in the search bar",       "ctrl+a"),
        ("copy",                                      "ctrl+c"),
        ("copy that",                                 "ctrl+c"),
        ("paste",                                     "ctrl+v"),
        ("undo",                                      "ctrl+z"),
        ("clear the search bar",                      "ctrl+a"),
    ]:
        press_log.clear(); hot_log.clear()
        r = brain._screen_control_fast_path(utt)
        check(f"{utt!r:42s} -> {expected_hot}",
              r is not None and expected_hot in (hot_log + press_log),
              f"r={r!r} hot={hot_log} press={press_log}")

# ── 4. Click center / corners (geometric, no vision) ─────────────────────
print("\n[4] Click center / corners (no vision call)")
clicks = []
with patch.object(screen_control.pyautogui, "click",
                  side_effect=lambda x, y, button="left": clicks.append((x, y))), \
     patch.object(screen_control, "get_screen_size", return_value=(2560, 1440)):
    clicks.clear()
    r = brain._screen_control_fast_path("click in the center of the screen")
    check("'click in the center'", clicks == [(1280, 720)], f"clicks={clicks}")
    clicks.clear()
    r = brain._screen_control_fast_path("click top-right")
    check("'click top-right'", len(clicks) == 1 and clicks[0][0] > 2000 and clicks[0][1] < 300,
          f"clicks={clicks}")

# ── 5. Single-word app fast-path ─────────────────────────────────────────
print("\n[5] Single-word app -> instant focus/open")
with patch.object(window_tools, "focus_window",
                  return_value={"ok": True, "title": "Discord"}):
    r = brain._screen_control_fast_path("discord")
    check("'discord' alone -> brought to front", r is not None and "Discord" in r, r)

# ── 6. focus_window prefers EXE match over title substring ───────────────
print("\n[6] focus_window: chrome must not match Discord (Electron-titled apps)")
# Simulate windows list: a Discord window AND a Chrome window
fake_wins = [
    {"hwnd": 101, "title": "@hunterslittleslave - Discord", "exe": "discord.exe", "pid": 10},
    {"hwnd": 202, "title": "Google", "exe": "chrome.exe", "pid": 20},
]
with patch.object(window_tools, "list_visible_windows", return_value=fake_wins):
    found = window_tools._find_hwnd_for("chrome")
    check("'chrome' -> hwnd 202 (Chrome), NOT 101 (Discord)",
          found is not None and found[0] == 202, found)
    found = window_tools._find_hwnd_for("discord")
    check("'discord' -> hwnd 101 (Discord)",
          found is not None and found[0] == 101, found)

# ── 7. Vision capture size is smaller ────────────────────────────────────
print("\n[7] Vision tuning")
check("MAX_DIMENSION reduced to 1024", vision_tools.MAX_DIMENSION == 1024)
check("VISION_TIMEOUT cut from 120 to 45", vision_tools.VISION_TIMEOUT == 45)

# ── 8. Vision cache invalidated by actions ───────────────────────────────
print("\n[8] Actions invalidate cache (so next vision sees fresh state)")
# Prime cache, then run scroll (mocked), then check cache is gone
with patch.object(vision_tools, "_capture_full_screen", side_effect=fake_capture):
    vision_tools.invalidate_cache()
    vision_tools._capture_b64()  # primes cache
with patch.object(screen_control.pyautogui, "scroll"), \
     patch.object(screen_control.pyautogui, "hscroll"):
    screen_control.scroll("down", 3)
check("scroll() invalidated cache", vision_tools._cached_b64 is None)

# ── Summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"RESULTS: {len(PASS)} pass, {len(FAIL)} fail")
print("=" * 60)
for n, d in FAIL: print(f"  FAIL: {n} - {d}")
sys.exit(0 if not FAIL else 1)
