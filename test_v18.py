"""V18 test suite — Hermes 3 swap, think toggle, stop button, barge-in."""
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
print("V18 TEST SUITE — Hermes 3, Think mode, Stop, Barge-in")
print("=" * 60)

# ── 1. Hermes 3 swap ─────────────────────────────────────────────────────
print("\n[1] Phase 1 — Hermes 3 swap")
import config
check("config.OLLAMA_MODEL is hermes3:8b",
      config.OLLAMA_MODEL == "hermes3:8b", config.OLLAMA_MODEL)

# Verify model exists in Ollama
import requests
try:
    r = requests.get("http://localhost:11434/api/tags", timeout=4)
    has = "hermes3" in r.text.lower()
    check("Ollama has hermes3 model pulled", has, f"http={r.status_code}")
except Exception as e:
    check("Ollama has hermes3", False, str(e))

# brain picks hermes3 (not anything else)
import brain
importlib.reload(brain)
brain.check_ollama()
check("brain.check_ollama() picks hermes3:8b",
      "hermes3" in brain._ollama_model_actual.lower(),
      brain._ollama_model_actual)

# ── 2. Think mode state ──────────────────────────────────────────────────
print("\n[2] Phase 2 — Think mode (state + voice triggers)")
import memory
importlib.reload(memory)
check("Default think mode is OFF", not memory.is_think_mode())
memory.set_think_mode(True)
check("set_think_mode(True) works", memory.is_think_mode())
memory.set_think_mode(False)
check("set_think_mode(False) works", not memory.is_think_mode())

# Voice triggers
importlib.reload(brain)
memory.set_think_mode(False)

for utt, expected_state, expected_reply_contains in [
    ("keep thinking",                  True, "on"),
    ("smart mode on",                  True, "on"),
    ("stay smart",                     True, "on"),
    ("stop thinking",                  False, "fast"),
    ("smart mode off",                 False, "fast"),
    ("go fast",                        False, "fast"),
]:
    memory.set_think_mode(not expected_state)   # set opposite to verify flip
    reply = brain._handle_voice_meta(utt)
    state_correct = memory.is_think_mode() == expected_state
    reply_correct = reply and expected_reply_contains in reply.lower()
    check(f"voice trigger {utt!r:30s} → state={expected_state}",
          state_correct and reply_correct, f"state={memory.is_think_mode()}, reply={reply}")

# Real commands should NOT trigger think toggle
for utt in ["open chrome", "scroll down", "what time is it", "weather in london"]:
    memory.set_think_mode(False)
    reply = brain._handle_voice_meta(utt)
    check(f"non-meta {utt!r:32s} → no toggle", reply is None and not memory.is_think_mode(),
          f"reply={reply}, think={memory.is_think_mode()}")

# ── 3. Stop voice triggers + memory flag ─────────────────────────────────
print("\n[3] Phase 3 — Stop button + voice phrases")
# voice stop
for utt in ["stop", "shut up", "be quiet", "quiet", "cancel", "nevermind"]:
    # Reset
    memory.consume_stop()
    reply = brain._handle_voice_meta(utt)
    check(f"voice {utt!r:22s} → stop", reply is not None and memory.is_stop_pending(),
          f"reply={reply}, stop={memory.is_stop_pending()}")

# request_stop + consume_stop semantics
memory.consume_stop()   # clear
check("consume_stop returns False when no stop", not memory.consume_stop())
memory.request_stop()
check("request_stop sets flag", memory.is_stop_pending())
check("consume_stop returns True then clears", memory.consume_stop() and not memory.is_stop_pending())

# voice.stop() exists
import voice
check("voice.stop() exists", hasattr(voice, "stop") and callable(voice.stop))

# ── 4. Barge-in plumbing ─────────────────────────────────────────────────
print("\n[4] Phase 4 — Barge-in interrupt")
import speech
check("AlwaysOn has signal_user_interrupt method",
      hasattr(speech.always_on, "signal_user_interrupt"))
# Verify the listener loop has barge-in code path
import inspect
src = inspect.getsource(speech._AlwaysOnListener._stream)
check("barge-in code path present in _stream",
      "barge_thr" in src and "signal_user_interrupt" in src)
check("barge-in requires sustained audio (8+ chunks)",
      "_barge_cnt >= 8" in src)

# ── 5. GUI has new buttons ───────────────────────────────────────────────
print("\n[5] GUI buttons (Think + Stop)")
import gui
src = inspect.getsource(gui.MakiWindow)
check("GUI has _think_btn", "_think_btn" in src)
check("GUI has _stop_btn", "_stop_btn" in src)
check("GUI has on_think_toggle callback wiring", "on_think_toggle" in src)
check("GUI has on_stop callback wiring", "on_stop" in src)
check("GUI has set_think() external setter", "def set_think" in src)

# ── 6. Existing test suites still pass ───────────────────────────────────
print("\n[6] No regressions — checking key existing tests")
# Quick smoke import test
try:
    import agent, intents, perception, intent_router
    importlib.reload(agent); importlib.reload(intents)
    importlib.reload(perception); importlib.reload(intent_router)
    check("All major modules still import cleanly", True)
except Exception as e:
    check("All major modules still import cleanly", False, str(e))

# ── Summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"RESULTS: {len(PASS)} pass, {len(FAIL)} fail")
print("=" * 60)
for n, d in FAIL: print(f"  FAIL: {n} - {d}")
sys.exit(0 if not FAIL else 1)
