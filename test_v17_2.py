"""V17.2 — fixes for V17.1 log bugs."""
from __future__ import annotations
import sys, importlib

try:
    from dotenv import load_dotenv; load_dotenv()
except Exception: pass

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(("  PASS  " if cond else "  FAIL  "), name, "-", str(detail)[:130])

print("=" * 60)
print("V17.2 TEST SUITE — V17.1 log fixes")
print("=" * 60)

import intents, perception
importlib.reload(intents); importlib.reload(perception)

# ── 1. _extract_city strips unit prefixes ────────────────────────────────
print("\n[1] _extract_city strips perception-output noise")
for utt, expected in [
    ("show the weather in celsius for Karachi, Egypt, and London",
     "karachi, egypt, and london"),
    ("show the weather in fahrenheit for Tokyo",
     "tokyo"),
    ("weather in london and pakistan in celsius",
     "london and pakistan"),
    ("weather in tokyo",
     "tokyo"),
]:
    got = intents._extract_city(utt)
    ok = got.lower().rstrip(".") == expected.lower().rstrip(".") or expected.lower() in got.lower()
    check(f"city({utt[:50]!r:54s}) → {expected!r}", ok, got)

# ── 2. _clean_title handles multi-dash titles ────────────────────────────
print("\n[2] _clean_title multi-dash")
for raw, expected in [
    ("gmail in google - Google Search - Google Chrome",  "Google Chrome"),
    ("Project foo - bar - baz - Visual Studio Code",     "Visual Studio Code"),
    ("Some Tab - Another Thing - Discord",               "Discord"),
    ("just text",                                         "just text"),
]:
    got = intents._clean_title(raw)
    check(f"clean({raw[:42]!r:48s}) → {expected!r}", got == expected, got)

# ── 3. get_time NOT triggered by "how are you" ───────────────────────────
print("\n[3] get_time intent properly gated")
router = intents.build_router()
router.prepare()
get_time = next((i for i in router._intents if i.name == "get_time"), None)
check("get_time threshold >= 0.80",
      get_time and get_time.threshold >= 0.80, get_time.threshold if get_time else None)
check("get_time has 'how are you' as negative",
      get_time and get_time.negatives and
      any("how are you" in n for n in get_time.negatives), "")

# Live router check
for it in router._intents:
    it.handler = (lambda name: lambda text: f"[ROUTED:{name}]")(it.name)
for utt, should_match_time in [
    ("how are you doing", False),
    ("how are you", False),
    ("what are you doing", False),
    ("what time is it", True),
    ("tell me the time", True),
]:
    r = router.route(utt)
    is_time = r == "[ROUTED:get_time]"
    check(f"router({utt!r:30s}) get_time={is_time}", is_time == should_match_time, r)

# ── 4. Perception skips greetings ────────────────────────────────────────
print("\n[4] Perception skips greetings (no garbage flag)")
for utt in [
    "hi, how are you doing",
    "hey how are you",
    "good morning",
    "thanks",
    "thank you",
]:
    skipped = perception._should_skip(utt)
    check(f"_should_skip({utt!r:30s}) == True", skipped, f"got {skipped}")

# Genuinely ambiguous things still go through perception (not skipped)
for utt in ["wikipedia", "a little crow", "school of chrome"]:
    skipped = perception._should_skip(utt)
    check(f"_should_skip({utt!r:24s}) == False (ambiguous → perceive)",
          not skipped, f"got {skipped}")

# ── 5. h_search_google strips fluff ──────────────────────────────────────
print("\n[5] h_search_google extracts clean query")
import web_tools
from unittest.mock import patch
captured = []
with patch.object(web_tools, "open_google_search",
                  side_effect=lambda q: captured.append(q) or f"search '{q}'"):
    for utt, expected_q in [
        ("search the web for labyrinth",                "labyrinth"),
        ("hey, give me information on labyrinth",       None),  # not a search verb pattern
        ("search for python tutorials",                 "python tutorials"),
        ("google how to cook pasta",                    "how to cook pasta"),
        ("search labyrinth on google",                  "labyrinth"),
        ("search the web for the best mid laner",       "the best mid laner"),
    ]:
        captured.clear()
        r = intents.h_search_google(utt)
        if expected_q is None:
            check(f"h_search_google({utt[:40]!r:43s}) → None", r is None, r)
        else:
            check(f"h_search_google({utt[:40]!r:43s}) → {expected_q!r}",
                  r is not None and captured and captured[0] == expected_q,
                  f"captured={captured}")

# ── 6. Multi-tab: 'open 3 tabs' opens three ──────────────────────────────
print("\n[6] Multi-tab support")
import screen_control
calls = []
with patch.object(screen_control, "press_keys",
                  side_effect=lambda c: calls.append(c) or f"Pressed {c}."):
    calls.clear()
    r = intents.h_new_tab("open 3 tabs")
    check("'open 3 tabs' → 3 presses",
          calls.count("ctrl+t") == 3 and "3" in str(r), f"calls={calls}, r={r}")
    calls.clear()
    r = intents.h_new_tab("create five tabs")
    check("'create five tabs' → 5 presses",
          calls.count("ctrl+t") == 5, f"calls={calls}")
    calls.clear()
    r = intents.h_new_tab("new tab")
    check("'new tab' → 1 press",
          calls.count("ctrl+t") == 1, f"calls={calls}")

# ── 7. _distill_app_name handles 'X in Y' ────────────────────────────────
print("\n[7] _distill_app_name 'X in Y' picks X")
for utt, expected in [
    ("gmail in google",     "gmail"),
    ("youtube in chrome",   "youtube"),
    ("chrome",              "chrome"),
    ("google chrome",       "google chrome"),
]:
    got = intents._distill_app_name(utt)
    check(f"distill({utt!r:28s}) → {expected!r}", got == expected, got)

# ── Summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"RESULTS: {len(PASS)} pass, {len(FAIL)} fail")
print("=" * 60)
for n, d in FAIL: print(f"  FAIL: {n} - {d}")
sys.exit(0 if not FAIL else 1)
