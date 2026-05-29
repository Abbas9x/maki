"""V17.1 — fixes for the V17 log bugs."""
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
print("V17.1 TEST SUITE — V17 log fixes")
print("=" * 60)

import intents
importlib.reload(intents)

# ── 1. _distill_app_name extracts known apps from noisy phrases ──────────
print("\n[1] _distill_app_name strips Whisper-mishearing noise")
for utt, expected in [
    ("brain chrome",                    "chrome"),
    ("ring chrome",                     "chrome"),
    ("ring chrome to front",            "chrome"),
    ("i'll put in whatsapp",            "whatsapp"),
    ("and use this on google chrome",   "google chrome"),
    ("the brain chrome",                "chrome"),
    ("chrome",                          "chrome"),
    ("google chrome",                   "google chrome"),
    ("random stuff",                    None),
    ("create a website",                None),
]:
    got = intents._distill_app_name(utt)
    check(f"distill({utt!r:38s}) → {expected!r}", got == expected, got)

# ── 2. h_focus_app uses distill correctly ─────────────────────────────────
print("\n[2] h_focus_app handles noisy phrases via distill")
import window_tools
from unittest.mock import patch
with patch.object(window_tools, "focus_window",
                  return_value={"ok": True, "title": "Google Chrome"}):
    # These previously said "Trying to open Brain Chrome" etc.
    for utt in ["brain chrome", "ring chrome to front"]:
        r = intents.h_focus_app(utt)
        check(f"h_focus_app({utt!r:30s}) → focuses Chrome",
              r is not None and "chrome" in r.lower() and "brain" not in r.lower()
              and "ring" not in r.lower(), r)

# ── 3. h_focus_app refuses non-app phrases ────────────────────────────────
print("\n[3] h_focus_app refuses non-app phrases")
for utt in [
    "type google studio",
    "search whatsapp on google",
    "can you create a website for me",
    "in the background",
    "what apps are running",
]:
    r = intents.h_focus_app(utt)
    check(f"h_focus_app({utt[:36]!r:38s}) → None", r is None, r)

# ── 4. h_get_weather rejects pronoun targets ──────────────────────────────
print("\n[4] h_get_weather refuses pronoun targets (falls through to agent)")
for utt in [
    "give me the weather for them",
    "weather in all of those countries",
    "weather for those",
    "weather in those cities",
]:
    r = intents.h_get_weather(utt)
    check(f"h_get_weather({utt[:36]!r:38s}) → None", r is None, r)

# Real cities still work
for utt in [
    "weather in london",
    "what's the weather in tokyo",
    "weather in london and pakistan",
]:
    r = intents.h_get_weather(utt)
    check(f"h_get_weather({utt[:36]!r:38s}) → real result", r is not None and
          "couldn't" not in str(r).lower(), str(r)[:80])

# ── 5. Perception prompt mentions pronoun expansion ───────────────────────
print("\n[5] Perception prompt forces pronoun expansion")
import perception
src = perception._PERCEPTION_SYSTEM
check("prompt mentions EXPAND PRONOUNS",
      "EXPAND PRONOUNS" in src or "expand pronouns" in src.lower(), "")
check("prompt has pronoun example",
      "weather for them" in src.lower() or "those countries" in src.lower(), "")

# ── 6. get_date threshold raised + has negatives ──────────────────────────
print("\n[6] get_date intent tightened")
router = intents.build_router()
get_date_intent = next((i for i in router._intents if i.name == "get_date"), None)
check("get_date intent exists", get_date_intent is not None)
check("get_date threshold >= 0.80",
      get_date_intent and get_date_intent.threshold >= 0.80,
      get_date_intent.threshold if get_date_intent else None)
check("get_date has negatives",
      get_date_intent and get_date_intent.negatives and
      any("what are you doing" in n for n in get_date_intent.negatives), "")

# ── Summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"RESULTS: {len(PASS)} pass, {len(FAIL)} fail")
print("=" * 60)
for n, d in FAIL: print(f"  FAIL: {n} - {d}")
sys.exit(0 if not FAIL else 1)
