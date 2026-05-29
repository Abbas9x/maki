"""V15.3 — fixes for V15.2 catastrophes from real log."""
from __future__ import annotations
import sys

try:
    from dotenv import load_dotenv; load_dotenv()
except Exception: pass

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(("  PASS  " if cond else "  FAIL  "), name, "-", str(detail)[:130])

print("=" * 60)
print("V15.3 TEST SUITE — Cerebras + intent fixes")
print("=" * 60)

import agent, intents

# ── 1. _strip_tool_call_junk removes raw JSON ────────────────────────────
print("\n[1] _strip_tool_call_junk removes raw JSON LLM emits")
cases = [
    ('{"tool":"press_keys","action":"ctrl+l"}{"tool":"type_text","text":"wikipedia"}{"tool":"press_keys","action":"enter"}All done!',
     "done"),
    ('Sure thing! {"tool":"browser.open","id":"0","url":"https://www.wikipedia.org"} Loading now.',
     "loading"),
    ('Normal answer with no JSON', "normal answer"),
    ('```python\nprint("hi")\n``` ok', "ok"),
]
for raw, want_contains in cases:
    cleaned = agent._strip_tool_call_junk(raw)
    has_json = '{"tool"' in cleaned or "```" in cleaned
    has_want = want_contains.lower() in cleaned.lower()
    check(f"strips: {raw[:50]!r}...", not has_json and has_want, cleaned[:100])

# Pure-junk input → returns clarifying message
junk_only = '{"tool":"foo"}{"tool":"bar"}'
cleaned = agent._strip_tool_call_junk(junk_only)
check("pure junk → friendly clarification",
      "rephrase" in cleaned.lower() or "tried to do" in cleaned.lower(), cleaned)

# ── 2. _extract_app strips ALL the variations from the log ───────────────
print("\n[2] _extract_app strips natural phrasings → 'chrome'")
for utt in [
    "bring chrome to front",
    "focus on chrome",
    "my chrome",
    "google chrome",
    "take me to chrome",
    "pull up chrome",
    "give me chrome",
    "open chrome",
    "switch to chrome",
    "go to chrome",
    "please bring chrome to front",
    "can you open chrome for me",
    "show me chrome",
    "jump to chrome",
]:
    got = intents._extract_app(utt)
    ok = got == "chrome" or got == "google chrome"
    check(f"{utt!r:40s} → 'chrome'", ok, got)

# Other apps too
for utt, expected in [
    ("bring discord to front",   "discord"),
    ("focus on spotify",         "spotify"),
    ("open whatsapp",            "whatsapp"),
    ("take me to youtube",       "youtube"),
]:
    got = intents._extract_app(utt)
    check(f"{utt!r:32s} → {expected!r}", got == expected, got)

# ── 3. Intent router routes correctly ────────────────────────────────────
print("\n[3] Live intent routing (catches the V15.2 log mistakes)")
router = intents.build_router()
router.prepare()
for it in router._intents:
    it.handler = (lambda name: lambda text: f"[ROUTED:{name}]")(it.name)

cases = [
    # The bugs:
    ("open youtube",          "focus_app"),    # was → search_youtube wrongly
    ("go to youtube",         "focus_app"),
    ("take me to youtube",    "focus_app"),
    ("bring chrome to front", "focus_app"),
    ("focus on chrome",       "focus_app"),
    ("my chrome",             "focus_app"),
    ("google chrome",         "focus_app"),

    # Search youtube still works for real searches
    ("search mrbeast on youtube",     "search_youtube"),
    ("look up music on youtube",      "search_youtube"),

    # Close still works
    ("close chrome",          "close_app"),
    ("quit discord",          "close_app"),
]
for utt, expected in cases:
    r = router.route(utt)
    ok = r == f"[ROUTED:{expected}]"
    check(f"{utt!r:40s} → {expected}", ok, r or "None")

# ── 4. Cerebras agent system prompt forbids JSON ─────────────────────────
print("\n[4] Cerebras agent prompt forbids JSON tool calls")
import inspect
src = inspect.getsource(agent.respond)
check("agent.respond uses CEREBRAS_AGENT_SYSTEM (not AGENT_SYSTEM)",
      "_CEREBRAS_AGENT_SYSTEM" in src)
check("Cerebras output is stripped via _strip_tool_call_junk",
      "_strip_tool_call_junk" in src)

# ── Summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"RESULTS: {len(PASS)} pass, {len(FAIL)} fail")
print("=" * 60)
for n, d in FAIL: print(f"  FAIL: {n} - {d}")
sys.exit(0 if not FAIL else 1)
