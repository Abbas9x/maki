"""V14.4 regression — the brutal log fixes."""
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
print("V14.4 TEST SUITE - log regression fixes")
print("=" * 60)

import brain, agent, screen_control, vision_tools, memory
importlib.reload(brain); importlib.reload(agent)
memory.set_last_weather(74.0, "F", "Islamabad")

# ── 1. convert_temp must NOT eat fresh weather queries ────────────────────
print("\n[1] convert_temp does NOT eat multi-city weather")
for utt in [
    "tell me the temperature in tokyo, london and new york in celsius",
    "tell me the weather in tokyo, london and new york in celsius",
    "weather in london and pakistan in celsius",
    "what's the temperature in islamabad in celsius",
]:
    d = brain._basic_classify(utt)
    is_weather = d and d.get("action") in ("get_weather", "get_weather_multi")
    check(f"{utt[:50]!r:55s} -> weather, NOT convert", is_weather,
          d.get("action") if d else None)

# Pure follow-ups MUST still hit convert_temp
print("\n[1b] Pure convert phrases still work")
for utt, want in [
    ("convert that to celsius", "C"),
    ("in celsius", "C"),
    ("convert them in celsius", "C"),
    ("show me celsius", "C"),
    ("make it celsius", "C"),
    ("what's that in c", "C"),
    ("in fahrenheit", "F"),
]:
    d = brain._basic_classify(utt)
    ok = d and d.get("action") == "convert_temp" and d.get("target") == want
    check(f"{utt!r:36s} -> convert_temp {want}", ok, d)

# ── 2. chitchat must NOT hallucinate actions ─────────────────────────────
print("\n[2] Chitchat rejects command-continuation fragments")
for utt in [
    "10 times",
    "a hundred times",
    "all the way",
    "more",
    "again",
    "down",
    "up",
    "keep going",
    "do that",
    "to the bottom",
]:
    check(f"NOT chitchat: {utt!r:20s}", not agent._is_chitchat(utt))

# Real chitchat is still chitchat
for utt in [
    "you don't even miss me",
    "i miss you",
    "how are you",
    "thanks",
]:
    check(f"IS chitchat: {utt!r:25s}", agent._is_chitchat(utt))

# Hallucination guard
check("_FAKE_ACTION_RE catches 'Scrolled down 100 times.'",
      bool(agent._FAKE_ACTION_RE.search("Scrolled down 100 times.")))
check("_FAKE_ACTION_RE catches 'Pressed enter.'",
      bool(agent._FAKE_ACTION_RE.search("Pressed enter.")))
check("_FAKE_ACTION_RE does NOT catch innocent chitchat",
      not agent._FAKE_ACTION_RE.search("I'm doing great, thanks for asking."))

# ── 3. Scroll loops + reports amount ──────────────────────────────────────
print("\n[3] Scroll reports amount + word numbers")
check("_parse_amount('a hundred times')==100",
      brain._parse_amount("a hundred times") == 100)
check("_parse_amount('hundred times')==100",
      brain._parse_amount("hundred times") == 100)
check("_parse_amount('twenty')==20",
      brain._parse_amount("twenty") == 20)
check("_parse_amount('twice')==2",
      brain._parse_amount("twice") == 2)
# response includes count
patches = []
for fn in ("scroll","new_tab","close_tab","reopen_tab","switch_tab",
           "browser_back","browser_forward","browser_refresh",
           "go_to_url","type_text","press_keys","click_text"):
    p = patch.object(screen_control, fn, side_effect=lambda *a, _n=fn, **kw: f"{_n}({a},{kw})")
    p.start(); patches.append(p)
try:
    r = brain._screen_control_fast_path("scroll up two times")
    check("'scroll up two times' -> scroll(up,2)",
          r is not None and "('up', 2)" in r, r)
    r = brain._screen_control_fast_path("scroll down a hundred times")
    check("'scroll down a hundred times' -> scroll(down,100)",
          r is not None and "('down', 100)" in r, r)
finally:
    for p in patches: p.stop()

# Actual screen_control.scroll reply mentions amount
with patch("pyautogui.scroll"), patch("pyautogui.hscroll"):
    r = screen_control.scroll("down", 100)
    check("scroll(down,100) reply mentions '100 times'",
          "100 times" in r, r)
    r = screen_control.scroll("up", 1)
    check("scroll(up,1) reply does NOT pluralize",
          r == "Scrolled up.", r)

# ── 4. Vision fast-path ──────────────────────────────────────────────────
print("\n[4] Vision fast-path bypasses agent")
import inspect
src = inspect.getsource(brain._screen_control_fast_path)
check("vision fast-path code present", "_VISION_RE" in src and "vision_tools" in src)
# Patch vision_tools.look_at_screen to capture the call
with patch.object(vision_tools, "look_at_screen",
                  side_effect=lambda q: f"VISION({q})"):
    for utt in [
        "what do you see on my screen",
        "look at my screen",
        "describe my screen",
        "tell me what's on my screen",
        "what is on my screen",
    ]:
        r = brain._screen_control_fast_path(utt)
        check(f"vision fast-path: {utt!r}",
              r is not None and r.startswith("VISION("), r)

# ── 5. Click fast-path ──────────────────────────────────────────────────
print("\n[5] Click fast-path bypasses agent (won't get mangled into press_keys)")
patches = []
for fn in ("click_text","press_keys"):
    p = patch.object(screen_control, fn, side_effect=lambda *a, _n=fn, **kw: f"{_n}({a})")
    p.start(); patches.append(p)
try:
    for utt, expect in [
        ("click on mohammed abbas profile",     "click_text"),
        ("press on mohammed's profile",         "click_text"),
        ("click the send button",               "click_text"),
        ("tap the github link",                 "click_text"),
        # real key presses still route to press_keys
        ("press enter",                         "press_keys"),
        ("hit escape",                          "press_keys"),
        ("press ctrl plus t",                   "press_keys"),
    ]:
        r = brain._screen_control_fast_path(utt)
        ok = r is not None and r.startswith(expect)
        check(f"{utt!r:42s} -> {expect}", ok, r)
finally:
    for p in patches: p.stop()

# ── Summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"RESULTS: {len(PASS)} pass, {len(FAIL)} fail")
print("=" * 60)
for n, d in FAIL: print(f"  FAIL: {n} - {d}")
sys.exit(0 if not FAIL else 1)
