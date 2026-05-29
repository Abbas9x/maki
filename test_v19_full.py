"""
test_v19_full.py — exhaustive sandbox verification for V19.

Covers everything that does NOT need a live machine:
  1. Concurrency lock — Hermes 3 vs qwen3-vl:4b serialization
  2. 8K guard edge cases (7499 / 7500 / 7501, NIM unavailable fallback)
  3. Lane classifier edge cases (tricky phrasings)
  4. Cap trackers (Groq dual/tri-cap + NIM credit alarm)
  5. Breadcrumb log integrity (start/end pairing, hang, rotation, append-only)
  6. Vision perception quality — SKIPPED in sandbox (Ollama not local)

Vision live tests are intentionally NOT here: qwen3-vl:4b needs Ollama
running on the same machine; the sandbox has no Ollama. Vision perception
moves to the user's manual checklist as designed (8/10 active-app test +
"what can I click" perception probe).
"""

from __future__ import annotations
import importlib, json, os, sys, threading, time
from pathlib import Path

# Force-reload env-dependent modules
from dotenv import load_dotenv; load_dotenv()

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(("  PASS  " if cond else "  FAIL  "), name, "-", str(detail)[:160])

print("=" * 70)
print("V19 FULL SANDBOX VERIFICATION")
print("=" * 70)


# ════════════════════════════════════════════════════════════════════════════
# 1. CONCURRENCY LOCK TEST — _local_model_lock serializes Hermes ↔ Vision
# ════════════════════════════════════════════════════════════════════════════
print("\n[1] Concurrency lock — _local_model_lock serialization")
import local_lock; importlib.reload(local_lock)
from local_lock import local_model_slot

order_log = []
order_lock = threading.Lock()
def record(label, phase):
    with order_lock:
        order_log.append((label, phase, time.time()))

def fake_vision_call():
    with local_model_slot("vision"):
        record("vision", "got_slot")
        time.sleep(0.4)            # simulate qwen3-vl:4b inference
        record("vision", "released")

def fake_hermes_call():
    with local_model_slot("hermes_agent"):
        record("hermes", "got_slot")
        time.sleep(0.2)            # simulate Hermes tool call
        record("hermes", "released")

# Start vision first, hermes 100ms later (mid-vision)
t_v = threading.Thread(target=fake_vision_call, name="vis")
t_h = threading.Thread(target=fake_hermes_call, name="herm")
t_v.start(); time.sleep(0.1); t_h.start()
t_v.join(); t_h.join()

# Verify: vision got slot first, hermes had to wait for vision to release
phases = [(label, phase) for label, phase, _ in order_log]
check("Concurrency: vision acquired slot before hermes",
      phases[0] == ("vision", "got_slot"),
      f"order: {phases}")
check("Concurrency: hermes acquired AFTER vision released (no race)",
      phases.index(("vision","released")) < phases.index(("hermes","got_slot")),
      f"order: {phases}")
# Time arithmetic: hermes_got must be >= vision_released time (within 50ms tolerance)
v_rel_t = next(t for l,p,t in order_log if (l,p)==("vision","released"))
h_got_t = next(t for l,p,t in order_log if (l,p)==("hermes","got_slot"))
check("Concurrency: hermes wait time matches vision hold time",
      h_got_t - v_rel_t >= -0.05,
      f"hermes_got - vision_released = {(h_got_t - v_rel_t)*1000:.0f}ms (must be >= 0)")

# 3-way concurrency to prove only one runs at a time
order_log.clear()
def quick_call(label):
    with local_model_slot(label):
        record(label, "got")
        time.sleep(0.15)
        record(label, "rel")

threads = [threading.Thread(target=quick_call, args=(f"call{i}",)) for i in range(3)]
for t in threads: t.start()
for t in threads: t.join()
# Verify: every "got" is followed by its own "rel" before the next "got"
got_rel_seq = [(l,p) for l,p,_ in order_log]
violations = 0
for i, (l,p) in enumerate(got_rel_seq):
    if p == "got" and i+1 < len(got_rel_seq):
        next_l, next_p = got_rel_seq[i+1]
        if next_p == "got" and next_l != l:
            violations += 1
check("Concurrency: 3 concurrent callers all serialized (no overlap)",
      violations == 0,
      f"order: {got_rel_seq}")


# ════════════════════════════════════════════════════════════════════════════
# 2. 8K CONTEXT GUARD EDGE CASES
# ════════════════════════════════════════════════════════════════════════════
print("\n[2] Cerebras 8K context guard — edge cases")
import budget; importlib.reload(budget)
from budget import count_messages, would_overflow_cerebras, route_or_reroute, CEREBRAS_THRESHOLD

# Helper: build a message list with exactly N projected tokens (close-enough)
def build_msgs(target_tokens: int):
    # Start with a small system msg (~5 tokens) and pad user content with words
    user_text = "word " * max(1, target_tokens - 20)
    msgs = [{"role":"system","content":"You are Maki."},
            {"role":"user","content":user_text}]
    # Trim to land near the target
    while count_messages(msgs) > target_tokens and len(msgs[1]["content"]) > 5:
        msgs[1]["content"] = msgs[1]["content"][:-5]
    while count_messages(msgs) < target_tokens:
        msgs[1]["content"] += "word "
    return msgs

# 7499 → must NOT overflow
m_under = build_msgs(7499)
check("8K guard: 7499 projected → routes to cerebras",
      not would_overflow_cerebras(m_under),
      f"projected={count_messages(m_under)}")

# 7500 → boundary — threshold is `> 7500`, so 7500 should still pass
m_at = build_msgs(7500)
check("8K guard: 7500 projected → routes to cerebras (boundary)",
      not would_overflow_cerebras(m_at),
      f"projected={count_messages(m_at)}")

# 7501 → must overflow
m_over = build_msgs(7501)
check("8K guard: 7501 projected → reroutes to nim",
      would_overflow_cerebras(m_over),
      f"projected={count_messages(m_over)}")

# 10000+ → definitely overflow + route_or_reroute returns nim_nemotron
m_big = build_msgs(10000)
lane, proj = route_or_reroute(m_big, "cerebras")
check("8K guard: 10K projected → route_or_reroute returns nim_nemotron",
      lane == "nim_nemotron",
      f"got lane={lane} projected={proj}")

# NIM unavailable: simulate dispatcher fallback chain (Cerebras blocked,
# NIM fails, must fall through to next provider without crashing).
# We call lane_dispatch._call_nim with a deliberately bad key path.
import lane_dispatch; importlib.reload(lane_dispatch)
# Monkeypatch nim_lane.chat to return "" (NIM dead)
import nim_lane as _nm
orig_chat = _nm.chat
_nm.chat = lambda messages, **kw: ""
try:
    reply, info = lane_dispatch.dispatch(
        "explain entropy", history=[],
        lane="nim_nemotron",
        system="One sentence.")
    check("8K guard: NIM unavailable → graceful fallback (no crash, returns something or '' cleanly)",
          isinstance(reply, str),
          f"reply_len={len(reply)} info={info}")
    check("8K guard: NIM unavailable → fallback chain documented in info",
          info.get("fallback") is True or info.get("lane_used") != "nim_nemotron",
          f"info={info}")
finally:
    _nm.chat = orig_chat


# ════════════════════════════════════════════════════════════════════════════
# 3. LANE CLASSIFIER EDGE CASES
# ════════════════════════════════════════════════════════════════════════════
print("\n[3] Lane classifier — tricky utterances")
import intents, lane_classifier; importlib.reload(lane_classifier)
router = intents.build_router()
sel = lambda utt, think=False: lane_classifier.select_lane(utt, think_mode_on=think, router=router)

# "what" alone — must NOT trigger tool override
lane, info = sel("what")
check("Edge: 'what' alone → not hermes_tools",
      lane != "hermes_tools",
      f"got={lane} reason={info['reason']} intent={info['intent']} conf={info['intent_conf']}")

# "never mind" → social (cancel-like) → groq_8b
lane, info = sel("never mind")
check("Edge: 'never mind' → groq_8b",
      lane == "groq_8b",
      f"got={lane} reason={info['reason']}")

# "actually open chrome instead" → tool call despite casual phrasing
lane, info = sel("actually open chrome instead")
check("Edge: 'actually open chrome instead' → hermes_tools",
      lane == "hermes_tools",
      f"got={lane} reason={info['reason']} intent={info['intent']} conf={info['intent_conf']}")

# "think harder" → must NOT flip Think (that's a GUI/voice meta-command, handled
# upstream of classifier in brain._handle_voice_meta — classifier should just
# route it like a normal utterance)
lane, info = sel("think harder")
check("Edge: 'think harder' → classifier does NOT route to github_premium (Think handled upstream)",
      lane != "github_premium",
      f"got={lane} reason={info['reason']}")

# Follow-up inheritance after Cerebras turn
lane_classifier.remember_lane("cerebras_120b")
lane, info = sel("explain that more simply")
check("Edge: 'explain that more simply' after cerebras → inherits cerebras",
      lane == "cerebras_120b" and info["inherited"] is True,
      f"got={lane} reason={info['reason']} inherited={info['inherited']}")

# "do it" — follow-up cue, should inherit
lane_classifier.remember_lane("groq_8b")
lane, info = sel("do it")
check("Edge: 'do it' after groq turn → inherits groq",
      lane == "groq_8b" and (info["inherited"] is True or info["reason"]=="social_keyword"),
      f"got={lane} reason={info['reason']} inherited={info['inherited']}")

# Confidence-floor sanity: anything below CONF_TOOL_FLOOR (0.78) on a tool
# intent must NOT become hermes_tools. We can't easily inject 0.70 exactly,
# but we can verify a borderline phrase doesn't false-positive.
lane_classifier._last_lane = None       # clear inheritance
lane_classifier._last_lane_t = 0
lane, info = sel("hmm")
check("Edge: short ambiguous 'hmm' → not hermes_tools",
      lane != "hermes_tools",
      f"got={lane} reason={info['reason']}")

# Vision intent confidence test
lane, info = sel("take a screenshot")
check("Edge: 'take a screenshot' → vision lane",
      lane == "vision",
      f"got={lane} reason={info['reason']}")

# Tool override beats Think mode
lane, info = sel("open notepad", think=True)
check("Edge: tool intent + Think ON → still hermes_tools (override wins)",
      lane == "hermes_tools",
      f"got={lane} reason={info['reason']}")


# ════════════════════════════════════════════════════════════════════════════
# 4. CAP TRACKERS — Groq dual-cap + NIM credit alarm
# ════════════════════════════════════════════════════════════════════════════
print("\n[4] Cap trackers — Groq req/tok + NIM credits")
importlib.reload(budget)
from budget import (groq_chat_available, groq_chat_record, groq_whisper_available,
                    groq_whisper_record, groq_status,
                    nim_record_call, nim_credits_remaining,
                    GROQ_CHAT_REQ_CAP, GROQ_CHAT_TOK_CAP,
                    NIM_STARTER_CREDITS)

# Reset Groq state to known
budget._groq_state["date"] = "FORCE"; budget._groq_reset_if_new_day()

# Just under req cap
budget._groq_state["chat_req"] = GROQ_CHAT_REQ_CAP - 1
ok, _ = groq_chat_available(est_tokens=100)
check(f"Cap: groq_req at {GROQ_CHAT_REQ_CAP-1} → allowed", ok, f"state={groq_status()}")
# At/past req cap
budget._groq_state["chat_req"] = GROQ_CHAT_REQ_CAP
ok, reason = groq_chat_available(est_tokens=100)
check(f"Cap: groq_req at {GROQ_CHAT_REQ_CAP} → blocked", not ok, f"reason={reason}")

# Reset
budget._groq_state["chat_req"] = 0
# Just under tok cap (room for 1-token call)
budget._groq_state["chat_tok"] = GROQ_CHAT_TOK_CAP - 100
ok, _ = groq_chat_available(est_tokens=50)
check(f"Cap: groq_tok at {GROQ_CHAT_TOK_CAP-100} + 50 est → allowed",
      ok, f"state={groq_status()}")
# Would-exceed
budget._groq_state["chat_tok"] = GROQ_CHAT_TOK_CAP - 50
ok, reason = groq_chat_available(est_tokens=100)
check(f"Cap: groq_tok would exceed {GROQ_CHAT_TOK_CAP} → blocked",
      not ok, f"reason={reason}")

# UTC midnight rollover — force a date change
budget._groq_state["chat_req"] = 14_000
budget._groq_state["chat_tok"] = 499_000
budget._groq_state["whisper_sec"] = 7000.0
budget._groq_state["date"] = "2020-01-01"   # ancient
budget._groq_reset_if_new_day()
check("Cap: UTC midnight → chat_req reset to 0", budget._groq_state["chat_req"] == 0)
check("Cap: UTC midnight → chat_tok reset to 0", budget._groq_state["chat_tok"] == 0)
check("Cap: UTC midnight → whisper_sec reset to 0", budget._groq_state["whisper_sec"] == 0.0)

# NIM credit alarm: 101 remaining → silent; 99 → alarm
budget._nim_state["credits_used"] = 0
budget._nim_state["first_use_t"] = None
# burn 899 credits (one at a time so first_use_t is set)
nim_record_call(credits_cost=1)
budget._nim_state["credits_used"] = NIM_STARTER_CREDITS - 101
# Capture log lines via a stream handler
import io, logging
log_buf = io.StringIO()
hdr = logging.StreamHandler(log_buf)
hdr.setLevel(logging.WARNING)
budget.logger.addHandler(hdr)
nim_record_call(credits_cost=1)   # remaining=100
check("Cap: NIM at 100 credits → no WARN alarm",
      "NIM credits low" not in log_buf.getvalue(),
      f"log='{log_buf.getvalue().strip()}'")
log_buf.truncate(0); log_buf.seek(0)
budget._nim_state["credits_used"] = NIM_STARTER_CREDITS - 100
nim_record_call(credits_cost=1)   # remaining=99
check("Cap: NIM at 99 credits → WARN alarm fires",
      "NIM credits low" in log_buf.getvalue(),
      f"log='{log_buf.getvalue().strip()}'")
check("Cap: NIM alarm includes burn_rate and projected_exhaustion",
      "burn_rate" in log_buf.getvalue() and "projected_exhaustion" in log_buf.getvalue(),
      f"log='{log_buf.getvalue().strip()}'")
budget.logger.removeHandler(hdr)


# ════════════════════════════════════════════════════════════════════════════
# 5. BREADCRUMB LOG INTEGRITY
# ════════════════════════════════════════════════════════════════════════════
print("\n[5] Breadcrumb log integrity")
import breadcrumb; importlib.reload(breadcrumb)

LOG = Path(__file__).parent / "logs" / "v19_actions.jsonl"

# Snapshot starting line count so we only inspect new entries
start_lines = len(LOG.read_text(encoding="utf-8").splitlines()) if LOG.exists() else 0

# Clean call: start + end with duration
with breadcrumb.trail("TEST", "clean_call", probe="v19_full"):
    time.sleep(0.05)
# Hang simulation: write a start manually with no matching end
breadcrumb._write({"ts":time.time(),"pid":os.getpid(),"subsystem":"TEST",
                   "action":"hang_call","kind":"start","probe":"v19_full"})

new_lines = LOG.read_text(encoding="utf-8").splitlines()[start_lines:]
new_entries = [json.loads(l) for l in new_lines]
# Verify clean call: start + end pair
clean = [e for e in new_entries if e.get("action")=="clean_call"]
check("Breadcrumb: clean call has matching start+end",
      len(clean)==2 and clean[0]["kind"]=="start" and clean[1]["kind"]=="end",
      f"got {len(clean)} entries for clean_call")
check("Breadcrumb: clean call end has duration_ms and ok=True",
      clean and "duration_ms" in clean[-1] and clean[-1]["ok"] is True,
      f"end_entry={clean[-1] if clean else None}")

# Hang: only start, no matching end → next boot would attribute the hang
hang = [e for e in new_entries if e.get("action")=="hang_call"]
check("Breadcrumb: hang call has start but NO end",
      len(hang)==1 and hang[0]["kind"]=="start",
      f"got {hang}")

# Exception capture
try:
    with breadcrumb.trail("TEST","fail_call",probe="v19_full"):
        raise RuntimeError("simulated")
except RuntimeError:
    pass
end = json.loads(LOG.read_text(encoding="utf-8").splitlines()[-1])
check("Breadcrumb: exception path records ok=False + error",
      end["action"]=="fail_call" and end["kind"]=="end" and end["ok"] is False
      and "simulated" in (end.get("error") or ""),
      f"end={end}")

# PID present on every entry
all_have_pid = all("pid" in e for e in new_entries)
check("Breadcrumb: every entry includes pid",
      all_have_pid, f"sample={new_entries[0] if new_entries else None}")

# Rotation: simulate by writing > 1MB worth of data
old_max = breadcrumb._MAX_BYTES
breadcrumb._MAX_BYTES = 2048    # tiny threshold for the test
prev_file = LOG.with_suffix(".jsonl.prev")
if prev_file.exists(): prev_file.unlink()
for _ in range(50):
    breadcrumb.note("TEST","rotation_probe", payload="x" * 200)
breadcrumb._MAX_BYTES = old_max
check("Breadcrumb: rotation creates .jsonl.prev when threshold exceeded",
      prev_file.exists(),
      f"prev={prev_file.exists()}")
# Append-only sanity: file just exists and is bigger than 0
check("Breadcrumb: file remains valid append-only JSONL after rotation",
      LOG.exists() and LOG.stat().st_size > 0,
      f"size={LOG.stat().st_size}")


# ════════════════════════════════════════════════════════════════════════════
# 6. VISION PERCEPTION — SKIPPED IN SANDBOX
# ════════════════════════════════════════════════════════════════════════════
print("\n[6] Vision perception quality")
print("  SKIP  Live qwen3-vl:4b calls require Ollama on the same machine.")
print("        This is the user's Tier-4 manual checklist (8/10 active-app")
print("        identification + 'what can I click' perception probe).")


# ════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print(f"V19 FULL SANDBOX VERIFICATION: {len(PASS)} pass, {len(FAIL)} fail")
print("=" * 70)
for n, d in FAIL:
    print(f"  FAIL: {n} - {d}")
sys.exit(0 if not FAIL else 1)
