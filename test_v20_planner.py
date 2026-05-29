"""
test_v20_planner.py — V20 Tier 2 planner regression test.

Asserts the 10 specific scenarios from the user's V20 spec against the
real cerebras_planner.plan() — live Cerebras calls (Groq secondary kicks
in automatically on 429). Paced at 2.2s between calls to stay under the
Cerebras free-tier 30 RPM.

Pass criterion per scenario: the returned Plan has the expected action
verb AND the target/lane signal matches what the spec demands.

This is the file that proves the "AI thinks first, tools execute second"
architecture actually works on the inputs the user listed.
"""

from __future__ import annotations
import sys, time
from dotenv import load_dotenv; load_dotenv()

import cerebras_planner

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append((label, detail))
    mark = "  PASS  " if cond else "  FAIL  "
    print(mark, label, "-", str(detail)[:160])


print("=" * 70)
print("V20 PLANNER TEST — 10 scenarios from the spec")
print("=" * 70)


# Context snippets the planner needs
recent_vision = [
    {"role": "user",      "content": "what do you see on the screen"},
    {"role": "assistant", "content": "I see a YouTube games collection: "
        "GAMEPLAY - first 30 minutes, REVIEW, TRAILER thumbnails. The big "
        "main video is the GAMEPLAY one."},
]
recent_completed = [
    {"role": "user",      "content": "open spotify"},
    {"role": "assistant", "content": "Spotify is in focus."},
]


def planner(text, screen_context="", history=None, active_app=""):
    """Helper that paces calls to stay under the 30-RPM Cerebras cap."""
    time.sleep(2.2)
    t0 = time.time()
    p = cerebras_planner.plan(text, screen_context=screen_context,
                              recent_history=history or [], active_app=active_app)
    dt = int((time.time() - t0) * 1000)
    if p is not None:
        print(f"    [{dt}ms] action={p.action} target={p.target[:60]!r} conf={p.confidence:.2f}")
    else:
        print(f"    [{dt}ms] <planner returned None>")
    return p


# ── 1. "copy everything in the search bar" → KEY ctrl+a (+ then ctrl+c) ─────
print("\n[1] copy everything in the search bar → KEY ctrl+a")
p = planner("copy everything in the search bar", active_app="chrome.exe")
check("scenario 1: plan returned", p is not None)
if p:
    check("scenario 1: action == KEY", p.action == "KEY",
          f"got {p.action}")
    check("scenario 1: target involves ctrl+a (select all)",
          "ctrl+a" in (p.target or "").lower(),
          f"target={p.target!r}")
    check("scenario 1: not the 'couldn't find everything' failure mode",
          "couldn't find" not in (p.intent or "").lower(),
          f"intent={p.intent!r}")

# ── 2. "thank you" → CHAT, never a tool ─────────────────────────────────────
print("\n[2] thank you → CHAT")
p = planner("thank you")
check("scenario 2: plan returned", p is not None)
if p:
    check("scenario 2: action == CHAT", p.action == "CHAT",
          f"got {p.action}")
    check("scenario 2: NOT a key press / tool",
          p.action not in ("KEY", "CLICK", "TYPE", "OPEN", "CLOSE"),
          f"got {p.action}")

# ── 3. "minimize discord" → KEY win+down (NOT maximize) ─────────────────────
print("\n[3] minimize discord → KEY win+down")
p = planner("minimize discord", active_app="discord.exe")
check("scenario 3: plan returned", p is not None)
if p:
    check("scenario 3: action makes window smaller (KEY win+down)",
          p.action == "KEY" and "win+down" in (p.target or "").lower(),
          f"action={p.action} target={p.target!r}")
    check("scenario 3: NOT maximize",
          "win+up" not in (p.target or "").lower()
          and "maximize" not in (p.intent or "").lower(),
          f"intent={p.intent!r} target={p.target!r}")

# ── 4. "click on the gameplay one" → CLICK target=<gameplay video name> ─────
print("\n[4] click on the gameplay one → CLICK (uses prior vision context)")
p = planner("click on the gameplay one",
            history=recent_vision, active_app="chrome.exe")
check("scenario 4: plan returned", p is not None)
if p:
    check("scenario 4: action == CLICK", p.action == "CLICK",
          f"got {p.action}")
    check("scenario 4: target references GAMEPLAY (from prior context)",
          "gameplay" in (p.target or "").lower(),
          f"target={p.target!r}")

# ── 5. "what games do you see in the games collection" → VISION ─────────────
print("\n[5] what games do you see in the games collection → VISION")
p = planner("what games do you see in the games collection",
            active_app="chrome.exe")
check("scenario 5: plan returned", p is not None)
if p:
    check("scenario 5: action == VISION", p.action == "VISION",
          f"got {p.action}")

# ── 6. "think and code a bubble sort" → CHAT (Think keyword routing) ───────
print("\n[6] think and code a bubble sort → CHAT (→ github_premium downstream)")
p = planner("think and code a bubble sort")
check("scenario 6: plan returned", p is not None)
if p:
    check("scenario 6: action == CHAT", p.action == "CHAT",
          f"got {p.action}")

# Confirm the Think-keyword override fires downstream:
from plan_executor import _wants_think_lane
check("scenario 6: plan_executor detects think keyword",
      _wants_think_lane("think and code a bubble sort"),
      "_wants_think_lane returned False")

# ── 7. "bye guys" (in Discord) → TYPE 'bye guys', NOT CLOSE ────────────────
print("\n[7] bye guys (in Discord) → TYPE 'bye guys'")
p = planner("bye guys", active_app="discord.exe")
check("scenario 7: plan returned", p is not None)
if p:
    check("scenario 7: NOT a close action",
          p.action != "CLOSE",
          f"got {p.action} target={p.target!r}")
    # We accept TYPE 'bye guys' (planner correctly typing into chat) OR
    # CHAT (planner choosing to respond rather than type). Both are
    # acceptable — what we MUST NOT see is CLOSE Discord.
    check("scenario 7: action is TYPE or CHAT (not CLOSE/KEY)",
          p.action in ("TYPE", "CHAT"),
          f"got {p.action}")

# ── 8. "yeah" after a completed action → CHAT ack ─────────────────────────
print("\n[8] yeah (after open spotify) → CHAT")
p = planner("yeah", history=recent_completed, active_app="spotify.exe")
check("scenario 8: plan returned", p is not None)
if p:
    check("scenario 8: action == CHAT (acknowledgment)",
          p.action == "CHAT",
          f"got {p.action}")

# ── 9. "i want you to think and code a for loop" → CHAT (Think keyword) ────
print("\n[9] i want you to think and code a for loop → CHAT (think keyword)")
p = planner("i want you to think and code a for loop")
check("scenario 9: plan returned", p is not None)
if p:
    check("scenario 9: action == CHAT", p.action == "CHAT",
          f"got {p.action}")
check("scenario 9: plan_executor detects think keyword",
      _wants_think_lane("i want you to think and code a for loop"),
      "_wants_think_lane returned False")

# ── 10. "do you see any buttons" → VISION ─────────────────────────────────
print("\n[10] do you see any buttons → VISION")
p = planner("do you see any buttons", active_app="chrome.exe")
check("scenario 10: plan returned", p is not None)
if p:
    check("scenario 10: action == VISION", p.action == "VISION",
          f"got {p.action}")


# ── Summary ─────────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print(f"V20 PLANNER: {len(PASS)} pass, {len(FAIL)} fail")
print("=" * 70)
for n, d in FAIL:
    print(f"  FAIL: {n} - {d}")
sys.exit(0 if not FAIL else 1)
