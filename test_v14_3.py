"""V14.3 — speed regressions from logs. All instant fast-paths."""
from __future__ import annotations
import sys, importlib
from unittest.mock import patch

try:
    from dotenv import load_dotenv; load_dotenv()
except Exception:
    pass

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(("  PASS  " if cond else "  FAIL  "), name, "—", str(detail)[:120])

print("=" * 60)
print("V14.3 SPEED FIX SUITE")
print("=" * 60)

# Reload modules so we pick up the edits
import brain, agent, screen_control
importlib.reload(brain); importlib.reload(agent)

# ── 1. Multi-city weather routes through fast-path ────────────────────────
print("\n[1] Multi-city weather/time fast-path")
d = brain._basic_classify("what's the weather in london and pakistan in celsius")
check("multi-city weather → action=get_weather_multi",
      d and d.get("action") == "get_weather_multi",
      d)
check("multi-city weather has confidence 1.0",
      d and d.get("confidence") == 1.0, d.get("confidence") if d else None)

d2 = brain._basic_classify("what time is it in tokyo and pakistan")
check("multi-city time → action=get_time_multi",
      d2 and d2.get("action") == "get_time_multi", d2)

# ── 2. Better scroll patterns ─────────────────────────────────────────────
print("\n[2] Scroll patterns (broad)")
patches = []
for fn in ("scroll","new_tab","close_tab","reopen_tab","switch_tab",
           "browser_back","browser_forward","browser_refresh",
           "go_to_url","type_text","press_keys"):
    p = patch.object(screen_control, fn,
                     side_effect=lambda *a, _n=fn, **kw: f"{_n}({a},{kw})")
    p.start(); patches.append(p)
try:
    cases = [
        ("scroll two times upwards",       "scroll(('up', 2)"),
        ("scroll up 10 times",             "scroll(('up', 10)"),
        ("scroll down 20 times",           "scroll(('down', 20)"),
        ("keep scrolling up",              "scroll(('up'"),
        ("scroll a bit down",              "scroll(('down'"),
        ("scroll three times down",        "scroll(('down', 3)"),
        ("scroll twice",                   "scroll(('down'"),
    ]
    for utt, want in cases:
        r = brain._screen_control_fast_path(utt)
        check(f"{utt!r:32s}", r is not None and want.split('(')[0] in r and want in r,
              r or "None")

    # V14.3 spec: non-key "press the X" returned None.
    # V14.4 IMPROVEMENT: now routes to click_text (vision/UIA click) directly.
    # Either is acceptable; what matters is it does NOT mangle into press_keys.
    print("\n[3] press_keys strictness — must NOT call press_keys with junk")
    # patch press_keys to detect any wrong call
    bad_press_calls = []
    with patch.object(screen_control, "press_keys",
                      side_effect=lambda c, **kw: bad_press_calls.append(c) or "BAD"):
        for utt in [
            "press the search icon",
            "press on the mohammed abbas profile",
            "press the send button",
            "press the close x",
        ]:
            bad_press_calls.clear()
            r = brain._screen_control_fast_path(utt)
            # Acceptable: None (deferred to agent) OR click_text was called.
            # Unacceptable: press_keys was called with a mangled combo.
            mangled = any("+" in c and "enter" not in c and "escape" not in c
                          and not all(p in ("ctrl","alt","shift","win","enter",
                                            "escape","tab","space") or len(p)==1
                                      for p in c.split("+"))
                          for c in bad_press_calls)
            check(f"NON-KEY {utt!r:36s} did NOT mangle press_keys",
                  not mangled, f"bad_calls={bad_press_calls}")

    # press_keys with REAL keys still works
    for utt, want in [
        ("press enter",           "press_keys(('enter',)"),
        ("press ctrl plus t",     "press_keys(('ctrl+t',)"),
        ("hit escape",            "press_keys(('escape',)"),
        ("press f5",              "press_keys(('f5',)"),
    ]:
        r = brain._screen_control_fast_path(utt)
        check(f"KEY {utt!r:36s}",
              r is not None and want in r, r or "None")
finally:
    for p in patches: p.stop()

# ── 4. Chitchat detection ─────────────────────────────────────────────────
print("\n[4] Chitchat detection (V14.3)")
for utt, expected in [
    ("you don't even miss me",        True),
    ("i miss you",                    True),
    ("hey how are you",               True),
    ("that's good",                   True),
    ("scroll down",                   False),     # command
    ("what's the weather in tokyo",   False),     # command
    ("look at my screen",             False),     # command
    ("open chrome",                   False),     # command
]:
    r = agent._is_chitchat(utt)
    check(f"chitchat({utt!r}) == {expected}", r == expected, f"got {r}")

# ── 5. Temperature conversion broader regex ──────────────────────────────
print("\n[5] Temp convert broader regex")
import memory
memory.set_last_weather(74.0, "F", "Islamabad")
for utt in [
    "convert that to celsius",
    "in celsius",
    "give me the temperature in celsius",
    "show me celsius",
    "make it celsius",
    "what's that in c",
]:
    d = brain._basic_classify(utt)
    ok = d and d.get("action") == "convert_temp" and d.get("target") == "C"
    check(f"{utt!r:40s} → convert_temp C", ok, d)

# ── 6. Weather with "tell me / can you tell me / give me" ────────────────
print("\n[6] Broader weather phrasing")
for utt in [
    "tell me the weather in islamabad",
    "can you tell me the temperature in islamabad",
    "give me the weather in london",
    "i want to know the weather in tokyo",
]:
    d = brain._basic_classify(utt)
    ok = d and d.get("action") == "get_weather"
    check(f"{utt!r:48s} → get_weather", ok, d)

# ── Summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"RESULTS: {len(PASS)} pass, {len(FAIL)} fail")
print("=" * 60)
for n, d in FAIL: print(f"  FAIL: {n} — {d}")
sys.exit(0 if not FAIL else 1)
