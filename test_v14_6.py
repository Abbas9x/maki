"""V14.6 — fixes for the catastrophes in the latest log.
Every test maps to a real log bug from pid 19196 session."""
from __future__ import annotations
import sys, importlib
from unittest.mock import patch

try:
    from dotenv import load_dotenv; load_dotenv()
except Exception: pass

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(("  PASS  " if cond else "  FAIL  "), name, "-", str(detail)[:130])

print("=" * 60)
print("V14.6 TEST SUITE — log catastrophe fixes")
print("=" * 60)

import brain, agent, screen_control, vision_tools, window_tools, safety, memory
importlib.reload(brain); importlib.reload(agent)
importlib.reload(screen_control); importlib.reload(safety)

# ── BUG #1: "go to chrome" must NOT close Chrome ──────────────────────────
print("\n[1] CRITICAL — close_app blocked unless user said close/quit")
agent._current_user_text = "go to chrome"
r = agent._execute_tool("close_app", {"name": "Chrome"})
check("close_app refused for 'go to chrome'", "refused" in r, r)

agent._current_user_text = "switch to chrome"
r = agent._execute_tool("close_app", {"name": "Chrome"})
check("close_app refused for 'switch to chrome'", "refused" in r, r)

agent._current_user_text = "open chrome"
r = agent._execute_tool("close_app", {"name": "Chrome"})
check("close_app refused for 'open chrome'", "refused" in r, r)

# When user DOES say close, allow it (but we mock actions.close_app)
agent._current_user_text = "close chrome"
with patch.object(agent.actions, "close_app", return_value="Closed Chrome."):
    r = agent._execute_tool("close_app", {"name": "Chrome"})
    check("close_app ALLOWED for 'close chrome'", "Closed Chrome" in r, r)

# ── BUG #2: bare-chord without 'press' prefix ─────────────────────────────
print("\n[2] Bare chords without 'press' prefix")
patches = []
press_log = []; hot_log = []
patches.append(patch.object(screen_control.pyautogui, "press",
                            side_effect=lambda k: press_log.append(k)))
patches.append(patch.object(screen_control.pyautogui, "hotkey",
                            side_effect=lambda *k: hot_log.append("+".join(k))))
for p in patches: p.start()
try:
    for utt, expected in [
        ("ctrl a",                "ctrl+a"),
        ("ctrl c",                "ctrl+c"),
        ("ctrl v",                "ctrl+v"),
        ("ctrl z",                "ctrl+z"),
        ("alt tab",               "alt+tab"),
        ("ctrl shift t",          "ctrl+shift+t"),
        ("ctrl a and backspace",  "ctrl+a"),   # multi-press
    ]:
        press_log.clear(); hot_log.clear()
        r = brain._screen_control_fast_path(utt)
        check(f"'{utt}' fast-path", r is not None and
              (expected in (hot_log + press_log) or expected in r), r)
finally:
    for p in patches: p.stop()

# ── BUG #3: "close the tab" → Ctrl+W ──────────────────────────────────────
print("\n[3] Tab/window control")
press_log.clear(); hot_log.clear()
for p in patches: p.start()
try:
    for utt, expected in [
        ("close the tab",           "ctrl+w"),
        ("close tab",               "ctrl+w"),
        ("close this tab",          "ctrl+w"),
        ("close the window",        "alt+f4"),
        ("reopen tab",              "ctrl+shift+t"),
    ]:
        press_log.clear(); hot_log.clear()
        r = brain._screen_control_fast_path(utt)
        check(f"'{utt}' fast-path", r is not None and
              (expected in (hot_log + press_log) or expected in r), r)
finally:
    for p in patches: p.stop()

# ── BUG #4: "go to chrome" routes to focus_window, NEVER close ────────────
print("\n[4] 'go to/switch to/take me to X' → focus or open, never close")
with patch.object(window_tools, "focus_window",
                  return_value={"ok": True, "title": "Google Chrome"}):
    for utt in [
        "go to chrome",
        "switch to chrome",
        "take me to discord",
        "show me spotify",
        "jump to vscode",
    ]:
        r = brain._screen_control_fast_path(utt)
        check(f"'{utt}' returns focus reply",
              r is not None and ("brought" in r.lower() or "front" in r.lower() or
                                  "open" in r.lower()),
              r)

# ── BUG #5: "delete" alone NOT flagged as risky ───────────────────────────
print("\n[5] Safety filter loosened (was over-blocking edit commands)")
for safe_utt in [
    "select all and delete",
    "ctrl a and backspace",
    "press delete",
    "hit backspace",
    "clear the search bar",
    "remove the last word",
]:
    check(f"'{safe_utt}' NOT risky", not safety.is_risky(safe_utt),
          f"safety.is_risky returned True")

# But genuinely destructive phrases STILL blocked
for risky_utt in [
    "delete file my_doc.txt",
    "format drive C",
    "wipe my disk",
    "send email to john",
    "buy this",
]:
    check(f"'{risky_utt}' STILL risky", safety.is_risky(risky_utt))

# ── BUG #6: filler-stripped vision phrases ────────────────────────────────
print("\n[6] Vision phrases tolerant to filler prefixes")
with patch.object(vision_tools, "look_at_screen", return_value="OK"):
    for utt in [
        "what? describe my screen",
        "um, what's on my screen",
        "uh, look at my screen",
        "well, describe my screen",
        "okay describe my screen",
        "hey what do you see",
    ]:
        r = brain._screen_control_fast_path(utt)
        check(f"vision fast-path: '{utt}'", r == "OK", r)

# ── BUG #7: short vision follow-ups ───────────────────────────────────────
print("\n[7] Short vision follow-ups still route to vision")
with patch.object(vision_tools, "look_at_screen", return_value="OK"):
    for utt in [
        "what is it",
        "what's it",
        "what is on my screen",
        "what does it say",
        "what's that on my screen",
        "analyze my screen",
    ]:
        r = brain._screen_control_fast_path(utt)
        check(f"vision short: '{utt}'", r == "OK", r)

# ── BUG #8: "select all in the search bar" → click first, then ctrl+a ─────
print("\n[8] 'select all IN X' → composite click + ctrl+a")
press_log.clear(); hot_log.clear()
with patch.object(screen_control, "click_text",
                  return_value="Clicked at (100,100)."), \
     patch.object(screen_control.pyautogui, "hotkey",
                  side_effect=lambda *k: hot_log.append("+".join(k))):
    hot_log.clear()
    r = brain._screen_control_fast_path("select all in the search bar")
    check("'select all in the search bar' clicks first",
          r is not None and "clicked" in r.lower() and "selected all" in r.lower()
          and "ctrl+a" in hot_log,
          f"r={r!r} hot={hot_log}")

# Plain "select all" still just ctrl+a (no clicking)
with patch.object(screen_control.pyautogui, "hotkey",
                  side_effect=lambda *k: hot_log.append("+".join(k))):
    hot_log.clear()
    r = brain._screen_control_fast_path("select all")
    check("'select all' alone → just ctrl+a",
          r is not None and "ctrl+a" in hot_log and "search" not in r.lower(),
          f"r={r!r} hot={hot_log}")

# ── Summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"RESULTS: {len(PASS)} pass, {len(FAIL)} fail")
print("=" * 60)
for n, d in FAIL: print(f"  FAIL: {n} - {d}")
sys.exit(0 if not FAIL else 1)
