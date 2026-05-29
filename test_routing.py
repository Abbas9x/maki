"""
test_routing.py — V19 Step 8

20 representative utterances -> asserted lane. Runs alongside test_v18.py.
This is the regression guard for the 6-lane router.

Lane shorthand:
  s = social        -> groq_8b
  k = knowledge     -> cerebras_120b
  d = deep          -> github_premium (only when Think mode is ON)
  t = tool          -> hermes_tools
  v = vision        -> vision
  i = inherit       -> previous turn's lane
"""

from __future__ import annotations
import sys, importlib

import intents, lane_classifier
importlib.reload(lane_classifier)
router = intents.build_router()

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append((label, detail))
    print(("  PASS  " if cond else "  FAIL  "), label, "-", str(detail)[:130])


print("=" * 60)
print("V19 routing test — 20 utterances -> 6 lanes")
print("=" * 60)

# ── 5 social -> groq_8b ───────────────────────────────────────────────────────
print("\n[A] Social -> groq_8b")
for utt in ["hi maki", "tell me a joke", "how are you", "thanks", "lol that's funny"]:
    lane, info = lane_classifier.select_lane(utt, think_mode_on=False, router=router)
    check(f"social {utt!r:32s} -> groq_8b", lane == "groq_8b",
          f"got={lane} reason={info['reason']}")

# ── 10 knowledge -> cerebras_120b ─────────────────────────────────────────────
print("\n[B] Knowledge -> cerebras_120b")
for utt in [
    "explain quantum tunneling",
    "what is the capital of japan",
    "summarize the iliad in one paragraph",
    "why is the sky blue",
    "how does an electric motor work",
    "what's the difference between TCP and UDP",
    "give me a one-sentence history of the roman empire",
    "what does 'serendipity' mean",
    "what's the boiling point of water on mount everest",
    "who wrote the lord of the rings",
]:
    lane, info = lane_classifier.select_lane(utt, think_mode_on=False, router=router)
    check(f"knowledge {utt[:36]!r:40s} -> cerebras_120b",
          lane == "cerebras_120b",
          f"got={lane} reason={info['reason']} intent={info['intent']}")

# ── 5 deep -> github_premium (Think mode ON) ──────────────────────────────────
print("\n[C] Deep + Think -> github_premium")
for utt in [
    "write me a 200-line python script that scrapes hacker news and ranks by upvotes",
    "debug this stack trace: AttributeError at line 47",
    "design a database schema for a saas billing system",
    "refactor this function to use async/await",
    "what's the proof that there are infinitely many primes",
]:
    lane, info = lane_classifier.select_lane(utt, think_mode_on=True, router=router)
    check(f"deep+think {utt[:30]!r:35s} -> github_premium",
          lane == "github_premium",
          f"got={lane} reason={info['reason']}")

# ── Tool-call override (always wins, even with Think on) ────────────────────
print("\n[D] Tool-call override")
for utt in ["open chrome", "close discord", "focus on spotify"]:
    lane, info = lane_classifier.select_lane(utt, think_mode_on=True, router=router)
    check(f"tool override {utt!r:32s} -> hermes_tools",
          lane == "hermes_tools",
          f"got={lane} reason={info['reason']}")

# ── Vision intent ────────────────────────────────────────────────────────────
print("\n[E] Vision intent")
for utt in ["take a screenshot", "what's on my screen"]:
    lane, info = lane_classifier.select_lane(utt, think_mode_on=False, router=router)
    check(f"vision {utt!r:32s} -> vision",
          lane == "vision",
          f"got={lane} reason={info['reason']}")

# ── Follow-up inheritance ────────────────────────────────────────────────────
print("\n[F] Follow-up inheritance (parent: cerebras_120b)")
lane_classifier.remember_lane("cerebras_120b")
for utt in ["simpler please", "go on", "elaborate"]:
    lane, info = lane_classifier.select_lane(utt, think_mode_on=False, router=router)
    check(f"inherit {utt!r:32s} -> cerebras_120b",
          lane == "cerebras_120b" and info.get("inherited") is True,
          f"got={lane} reason={info['reason']} inherited={info.get('inherited')}")

# ── Summary ─────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"V19 routing: {len(PASS)} pass, {len(FAIL)} fail")
print("=" * 60)
for n, d in FAIL:
    print(f"  FAIL: {n} - {d}")
sys.exit(0 if not FAIL else 1)
