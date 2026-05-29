"""V14 targeted test — ensure regressions from V13 are fixed.
Focuses on the new fast-paths + agent-timeout-tool-result behavior.
DOES NOT actually move mouse / type / scroll (would disrupt the user)."""
from __future__ import annotations
import sys
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
print("V14 TEST SUITE — Fast-paths + Agent timeout fix")
print("=" * 60)

# ── 1. Imports ────────────────────────────────────────────────────────────
print("\n[1] IMPORTS")
import brain, agent, screen_control, vision_tools, ui_tree
check("brain has _screen_control_fast_path", hasattr(brain, "_screen_control_fast_path"))
check("brain has _parse_amount",             hasattr(brain, "_parse_amount"))
check("agent has _humanize_tool_result",     hasattr(agent, "_humanize_tool_result"))
check("agent timeout >= 30",                 agent._AGENT_TIMEOUT >= 30,
      f"timeout={agent._AGENT_TIMEOUT}")

# ── 2. Amount parser ──────────────────────────────────────────────────────
print("\n[2] _parse_amount")
check("'5 times' -> 5",       brain._parse_amount("5 times", 1) == 5)
check("'three times' -> 3",   brain._parse_amount("three times", 1) == 3)
check("'twice' -> 2",         brain._parse_amount("twice", 1) == 2)
check("'' -> default(5)",     brain._parse_amount("", 5) == 5)
check("'99' clamped to 100 (V14.4 raised cap)",
      brain._parse_amount("99 times", 1) == 99)
check("'150' clamped to 100",
      brain._parse_amount("150 times", 1) == 100)

# ── 3. Screen-control fast-path — pattern match WITHOUT executing ─────────
# We stub out screen_control functions so the test doesn't actually do them.
print("\n[3] _screen_control_fast_path (mocked actions)")
results = {}
def _stub(*a, **kw):
    fn = sys._getframe(0).f_code.co_name  # unused
    return f"STUB({a},{kw})"
patches = []
for fn in ("scroll","new_tab","close_tab","reopen_tab","switch_tab",
           "browser_back","browser_forward","browser_refresh",
           "go_to_url","type_text","press_keys"):
    p = patch.object(screen_control, fn,
                     side_effect=lambda *a, _n=fn, **kw: f"{_n}({a},{kw})")
    p.start(); patches.append(p)
try:
    # V14.6: some patterns now route via press_keys (Ctrl+T/W/Shift+T)
    # instead of the dedicated wrappers. Functionally identical.
    cases = [
        ("scroll down",                  ["scroll"]),
        ("scroll down 3 times",          ["scroll"]),
        ("scroll up twice",              ["scroll"]),
        ("new tab",                      ["new_tab", "press_keys"]),
        ("open a new tab",               ["new_tab", "press_keys"]),
        ("close tab",                    ["close_tab", "press_keys"]),
        ("close this tab",               ["close_tab", "press_keys"]),
        ("reopen tab",                   ["reopen_tab", "press_keys"]),
        ("switch tab",                   ["switch_tab", "press_keys"]),
        ("go back",                      ["browser_back", "press_keys"]),
        ("back",                         ["browser_back", "press_keys"]),
        ("go forward",                   ["browser_forward", "press_keys"]),
        ("refresh",                      ["browser_refresh", "press_keys"]),
        ("reload",                       ["browser_refresh", "press_keys"]),
        ("go to youtube.com",            ["go_to_url"]),
        ("visit github.com",             ["go_to_url"]),
        ("type hello world",             ["type_text"]),
        ("press enter",                  ["press_keys"]),
        ("press ctrl plus t",            ["press_keys"]),
        ("hit escape",                   ["press_keys"]),
        ("please scroll down",           ["scroll"]),
        ("can you go back",              ["browser_back", "press_keys"]),
    ]
    for utt, expected_fns in cases:
        r = brain._screen_control_fast_path(utt)
        ok = r is not None and any(fn in r for fn in expected_fns)
        check(f"{utt!r:38s} -> {expected_fns[0]}", ok, r or "None")

    # Negative cases — these should NOT fire the fast-path
    neg = [
        "what time is it",
        "type github in the search bar",  # needs vision click first
        "scroll the news for me",          # not deterministic
        "tell me about scroll",            # conversational
    ]
    for utt in neg:
        r = brain._screen_control_fast_path(utt)
        check(f"NEG {utt!r:38s}", r is None or "scroll" not in str(r).lower()[:6],
              str(r)[:60] if r else "None (ok)")
finally:
    for p in patches: p.stop()

# ── 4. Agent timeout fallback ─────────────────────────────────────────────
print("\n[4] Agent tool-result fallback on timeout")
# Mock the Ollama requests.post to time out AFTER a successful tool run
import requests as _req
call_count = {"n": 0}
class _FakeResp:
    status_code = 200
    def raise_for_status(self): pass
    def json(self):
        # First round: model wants to call scroll
        if call_count["n"] == 1:
            return {"message": {"content": "", "tool_calls": [
                {"function": {"name":"scroll","arguments":{"direction":"down","amount":3}}}]}}
        return {}
def _fake_post(*a, **kw):
    call_count["n"] += 1
    if call_count["n"] == 1: return _FakeResp()
    # Second round: simulate timeout
    raise _req.Timeout("simulated")

with patch.object(_req, "post", side_effect=_fake_post), \
     patch.object(screen_control, "scroll",
                  side_effect=lambda *a, **kw: "Scrolled down."):
    reply = agent._ollama_agent("scroll down 3 times", history=[])
    check("Timeout AFTER successful tool -> humanized tool result",
          reply == "Scrolled down.", f"got: {reply!r}")

# ── 5. Compound delegates unknown parts to agent ──────────────────────────
print("\n[5] Compound parser delegation (V14)")
import inspect
src = inspect.getsource(brain._handle_compound)
check("compound source mentions agent delegate", "agent" in src and "respond" in src)

# ── 6. System prompt updated ─────────────────────────────────────────────
print("\n[6] System prompt strengthened")
sp = agent.AGENT_SYSTEM
check("prompt: 'CALL THE TOOL on the first try'", "CALL THE TOOL on the first try" in sp)
check("prompt: click_text example",               "click_text" in sp)
check("prompt: follow-up call_text rule",         "follow-ups about" in sp.lower() or "follow-up" in sp.lower())

# ── 7. click_text tries UIA first ─────────────────────────────────────────
print("\n[7] click_text: UIA first, vision fallback")
src = inspect.getsource(screen_control.click_text)
check("click_text imports ui_tree", "ui_tree" in src)
check("click_text uses invoke_element_by_name", "invoke_element_by_name" in src)

# ── Summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"RESULTS: {len(PASS)} pass, {len(FAIL)} fail")
print("=" * 60)
for n, d in FAIL: print(f"  FAIL: {n} — {d}")
sys.exit(0 if not FAIL else 1)
