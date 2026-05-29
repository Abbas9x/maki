"""V15 — semantic intent routing live test."""
from __future__ import annotations
import sys, time
try:
    from dotenv import load_dotenv; load_dotenv()
except Exception: pass

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(("  PASS  " if cond else "  FAIL  "), name, "-", str(detail)[:130])

print("=" * 60)
print("V15 SEMANTIC INTENT ROUTING")
print("=" * 60)

# Import + build router (this will embed all ~150 example phrases)
import intents, intent_router
router = intents.build_router()
print(f"\nBuilding embeddings for {sum(len(i.examples) for i in router._intents)} example phrases...")
t0 = time.time()
router.prepare()
print(f"  ready in {time.time()-t0:.1f}s ({len(router._example_vecs)} vectors)")

# Mock all the handlers to just return the intent name
for it in router._intents:
    it.handler = (lambda name: lambda text: f"[ROUTED:{name}]")(it.name)

# ── Test natural phrasings that REGEX never caught ───────────────────────
print("\n[1] Natural phrasings (regex couldn't catch these all)")
cases = [
    # focus_app — all the ways people say "give me chrome"
    ("go to chrome",              "focus_app"),
    ("take me to chrome",         "focus_app"),
    ("switch to chrome",          "focus_app"),
    ("bring chrome up",           "focus_app"),
    ("show me discord",           "focus_app"),
    ("pull up spotify",           "focus_app"),
    ("jump to vscode",            "focus_app"),
    ("give me chrome",            "focus_app"),

    # close_app — must NOT be triggered by "open/go to" variants
    ("close chrome",              "close_app"),
    ("quit discord",              "close_app"),
    ("kill chrome",               "close_app"),
    ("shut down spotify",         "close_app"),

    # scroll
    ("scroll down ten times",     "scroll"),
    ("scroll up a hundred times", "scroll"),
    ("keep scrolling down",       "scroll"),
    ("scroll to the bottom",      "scroll"),
    ("scroll a bit",              "scroll"),
    ("page down",                 "scroll"),

    # tab control
    ("close this tab",            "close_tab"),
    ("open a new tab",            "new_tab"),
    ("bring back the tab i closed", "reopen_tab"),
    ("go back",                   "browser_back"),
    ("refresh the page",          "refresh"),

    # edit shortcuts
    ("select all",                "select_all"),
    ("select everything",         "select_all"),
    ("copy that",                 "copy"),
    ("paste it here",             "paste"),
    ("undo that",                 "undo"),

    # vision
    ("what's on my screen",       "look_at_screen"),
    ("look at my screen",         "look_at_screen"),
    ("what do you see",           "look_at_screen"),
    ("describe my screen",        "describe_screen"),
    ("read this for me",          "read_screen"),

    # click element
    ("click the send button",     "click_element"),
    ("press on muhammad abbas profile", "click_element"),
    ("tap the github link",       "click_element"),

    # weather / time
    ("what's the weather in tokyo", "get_weather"),
    ("temperature in islamabad",    "get_weather"),
    ("weather in london and pakistan", "get_weather"),
    ("what time is it",             "get_time"),
    ("what time is it in tokyo",    "time_in_place"),
]
# Sometimes two intents are functionally equivalent (look_at_screen vs
# describe_screen both call vision). Accept either.
_EQUIV = {
    "look_at_screen": {"look_at_screen", "describe_screen", "read_screen"},
    "describe_screen": {"describe_screen", "look_at_screen"},
    "read_screen": {"read_screen", "look_at_screen"},
    "focus_app": {"focus_app", "open_app"},
    "open_app": {"open_app", "focus_app"},
}
total_time = 0.0
for utt, expected_intent in cases:
    t = time.time()
    r = router.route(utt)
    dt = time.time() - t
    total_time += dt
    acceptable = _EQUIV.get(expected_intent, {expected_intent})
    ok = r in {f"[ROUTED:{i}]" for i in acceptable}
    check(f"{utt!r:48s} → {expected_intent} ({dt*1000:.0f}ms)", ok, r or "None")

print(f"\nAverage route time: {total_time/len(cases)*1000:.0f}ms")

# ── Negative tests — should NOT match (returns None) ─────────────────────
print("\n[2] Negative cases (should fall through, not match)")
neg_cases = [
    "tell me a joke about cats",                # conversational, no intent
    "i'm feeling really tired today",           # chitchat
    "do you know how to solve this math problem", # general knowledge
    "what's the meaning of life",               # philosophy
    "write me a poem about chrome",             # creative — must not trigger focus_app
]
for utt in neg_cases:
    r = router.route(utt)
    check(f"NEG {utt!r:55s}", r is None, r or "None (ok)")

# ── Open vs close (the disambiguation trap) ──────────────────────────────
print("\n[3] Open vs close disambiguation (semantic risk)")
for utt, expected in [
    ("close chrome",   "close_app"),
    ("open chrome",    "focus_app"),     # open/go-to should focus, not match close
    ("go to chrome",   "focus_app"),
    ("quit chrome",    "close_app"),
]:
    r = router.route(utt)
    ok = r == f"[ROUTED:{expected}]"
    check(f"'{utt}' → {expected}", ok, r or "None")

# ── Summary ─────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"RESULTS: {len(PASS)} pass, {len(FAIL)} fail")
print("=" * 60)
for n, d in FAIL: print(f"  FAIL: {n} - {d}")
sys.exit(0 if not FAIL else 1)
