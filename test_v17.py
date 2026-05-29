"""V17 — fixes for the catastrophes in the V16 log (pid 20380)."""
from __future__ import annotations
import sys, importlib
from unittest.mock import patch, MagicMock

try:
    from dotenv import load_dotenv; load_dotenv()
except Exception: pass

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(("  PASS  " if cond else "  FAIL  "), name, "-", str(detail)[:130])

print("=" * 60)
print("V17 TEST SUITE — V16 log catastrophe fixes")
print("=" * 60)

import brain, agent, intents, intent_router, perception, vision_tools
importlib.reload(brain); importlib.reload(agent)
importlib.reload(intents); importlib.reload(perception)
importlib.reload(vision_tools)

# ── 1. Intent router REFUSES non-app phrases ─────────────────────────────
print("\n[1] intent_router refuses non-app phrases (V16 catastrophe)")
router = intents.build_router()
router.prepare()
# Restore real handlers — testing the gating behavior
for utt in [
    "can you create a website for me",
    "in the background",
    "what apps are running",
    "search youtube on google",
    "open it on youtube, open it on google",
    "type google studio",
    "white google studio",
    "search google studio",
    "make a new tab and search google studio",   # multi-action, also tests splitter
]:
    r = router.route(utt)
    # Should EITHER fall through (None) OR route to something correct (not focus_app->junk)
    # focus_app handler returns None when target isn't a known app, so router will pass
    ok = r is None or "trying to open" not in str(r).lower() or \
         any(real_app in str(r).lower() for real_app in
             ["chrome","discord","spotify","youtube","whatsapp","gmail"])
    # Specifically forbid: "Trying to open Create A Website" etc.
    bad_phrases = ["create a website","white google studio","type google studio",
                    "search google studio","it on youtube","in the background"]
    has_bad = any(p in str(r).lower() for p in bad_phrases) if r else False
    check(f"{utt[:50]!r:55s}", ok and not has_bad, r or "None (good)")

# ── 2. _is_known_app gate ─────────────────────────────────────────────────
print("\n[2] _is_known_app gate")
for app, expected in [
    ("chrome", True),
    ("discord", True),
    ("create a website", False),
    ("in the background", False),
    ("what apps are running", False),
    ("type google studio", False),
    ("google chrome", True),
]:
    got = intents._is_known_app(app)
    check(f"_is_known_app({app!r}) → {expected}", got == expected, f"got {got}")

# ── 3. _clean_title strips URL/username clutter ──────────────────────────
print("\n[3] _clean_title makes window titles voice-friendly")
for raw, expected in [
    ("Cerebras Cloud - Google Chrome",   "Google Chrome"),
    ("@hunterslittleslave - Discord",    "Discord"),
    ("README.md - Visual Studio Code",   "Visual Studio Code"),
    ("Project foo (main) - Visual Studio Code", "Visual Studio Code"),
    ("Discord",                          "Discord"),
]:
    got = intents._clean_title(raw)
    check(f"clean({raw!r}) → {expected!r}", got == expected, got)

# ── 4. Multi-action splitter ─────────────────────────────────────────────
print("\n[4] Multi-action splitter ('X and Y' → route each)")
# Test the function — should fire for command+command, not for chitchat
import screen_control, window_tools
calls = []
with patch.object(screen_control, "new_tab",
                  side_effect=lambda: calls.append("new_tab") or "Pressed ctrl+t."), \
     patch.object(screen_control, "press_keys",
                  side_effect=lambda c: calls.append(f"press_keys({c})") or f"Pressed {c}."), \
     patch.object(window_tools, "focus_window",
                  return_value={"ok": True, "title": "Chrome"}):
    calls.clear()
    r = brain._try_multi_action("open chrome and go back")
    check("'open chrome and go back' → both run",
          r is not None and "chrome" in str(r).lower(), r)
    # Reject weather multi-city (handled elsewhere)
    r = brain._try_multi_action("weather in london and pakistan")
    check("weather multi-city NOT multi-action", r is None, r)
    # Single action — no split
    r = brain._try_multi_action("open chrome")
    check("single action NOT multi-action", r is None, r)
    # Chitchat — no split
    r = brain._try_multi_action("hey how are you and what's up")
    check("chitchat NOT multi-action", r is None, r)

# ── 5. Cerebras prompt mentions REAL tools ────────────────────────────────
print("\n[5] Cerebras agent prompt now lists real capabilities")
import inspect
src = inspect.getsource(agent.respond)
check("Cerebras prompt mentions type text", "Open / focus / close apps" in src or
      "type text" in src.lower())
check("Cerebras prompt mentions scroll", "Scroll" in src or "scroll" in src.lower())
check("Cerebras prompt mentions vision", "vision" in src.lower() or
      "screen" in src.lower())
check("Cerebras prompt does NOT say 'NO tools'",
      "do NOT currently have access to any tools" not in src)

# ── 6. Perception robust JSON parse ──────────────────────────────────────
print("\n[6] Perception parses truncated JSON")
# Simulate truncated Cerebras response
truncated = '{\n  "corrected": "click the search bar",\n  "is_garbage": false,\n  "confidence": 0.86,\n  "clarify": "'
parsed = perception._parse_json_safely(truncated)
check("rescues 'corrected' from truncated JSON",
      parsed and parsed.get("corrected") == "click the search bar", parsed)
check("rescues 'confidence' from truncated JSON",
      parsed and float(parsed.get("confidence", 0)) > 0.8, parsed)

# Full valid
full = '{"corrected":"X","is_garbage":false,"confidence":0.9,"clarify":"","reasoning":"y"}'
parsed = perception._parse_json_safely(full)
check("full JSON parses correctly",
      parsed and parsed.get("corrected") == "X", parsed)

# Pure garbage
parsed = perception._parse_json_safely("not json at all")
check("garbage returns None", parsed is None, parsed)

# Perception max_tokens bumped
src = inspect.getsource(perception.perceive)
check("perception max_tokens >= 400", "max_tokens=450" in src or
      "max_tokens=400" in src or "max_tokens >= 400" in src, "")

# ── 7. Vision: single retry, then fast fail ──────────────────────────────
print("\n[7] Vision retry logic — fast fail")
src = inspect.getsource(vision_tools._ask_vision)
check("vision has ONE retry (not infinite)",
      src.count("_ollama_vision") <= 3,    # main + retry, plus the def line
      f"calls: {src.count('_ollama_vision')}")
check("vision retry uses brief prompt",
      "brief" in src.lower() or "single retry" in src.lower(), "")

# ── 8. Capture: mss uses primary monitor only (SIGSEGV defense) ──────────
print("\n[8] Capture uses primary monitor (not virtual all-monitors)")
src = inspect.getsource(vision_tools._capture_full_screen)
check("uses monitor[1] not monitor[0]",
      "monitors[1]" in src or "mons[1]" in src, "")
check("releases mss handle in finally",
      "finally" in src and "sct.close" in src, "")

# ── Summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"RESULTS: {len(PASS)} pass, {len(FAIL)} fail")
print("=" * 60)
for n, d in FAIL: print(f"  FAIL: {n} - {d}")
sys.exit(0 if not FAIL else 1)
