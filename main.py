"""
main.py - Maki V2, always-on conversational agent.

Pipeline per utterance:
  listen → clean transcript → brain.process() → reply
  brain handles: clarification, confirmation, tool execution, conversation.
  Garbled / very short transcripts prompt a "can you repeat" before processing.
"""

import logging, os as _envset, queue, re, sys, threading, time, random

# ── V19 BUG-1b FIX: cap Ollama at 1 loaded model at a time ──────────────────
# On an 8 GB card hermes3:8b (4.7 GB) + qwen3-vl:4b (3.3 GB) = 8 GB → won't
# coexist. OLLAMA_MAX_LOADED_MODELS=1 tells Ollama to unload the current
# resident model when a different one is requested. This is the documented
# Ollama way to enforce single-model VRAM and replaces the manual
# _evict_chat_model() workaround which raced under sustained use.
# MUST be set BEFORE any module imports requests-to-Ollama (Ollama reads
# this on each request).
_envset.environ.setdefault("OLLAMA_MAX_LOADED_MODELS", "1")
# Also keep keep_alive short so the resident model doesn't camp on the card
# longer than needed.
_envset.environ.setdefault("OLLAMA_KEEP_ALIVE", "5m")

# ── V19 BUG-4b FIX: disable PyAutoGUI fail-safe ────────────────────────────
# The mouse-to-(0,0) fail-safe was firing on legitimate utterances ("thank
# you", "let's get to") because random mouse moves during normal use brushed
# the corner. The real safety net is the safety.is_risky check + user
# confirmation, not a hair-trigger mouse-position check.
try:
    import pyautogui as _pag
    _pag.FAILSAFE = False
except Exception:
    pass
try:
    import pydirectinput as _pdi
    _pdi.FAILSAFE = False
except Exception:
    pass

# ── V19: auto-start Ollama if it isn't already running ─────────────────────
# Must run BEFORE `import brain` etc., because those modules ping Ollama on
# load. No popup window on Windows, doesn't kill Ollama on exit (it stays
# up as a system service), no crash if Ollama isn't installed — Maki keeps
# going with cloud fallbacks.
def _ensure_ollama_running(wait_s: int = 10) -> bool:
    """Returns True if Ollama is responsive (already running or successfully
    started here). False = we tried and gave up; Maki continues anyway."""
    import subprocess, requests, time as _t
    OLLAMA_URL = "http://localhost:11434/api/tags"

    # 1. Already running?
    try:
        if requests.get(OLLAMA_URL, timeout=2).status_code == 200:
            return True
    except Exception:
        pass

    # 2. Try to spawn `ollama serve` silently in the background.
    print("[Maki] Ollama not running — starting it silently in background...",
          flush=True)
    try:
        _CREATE_NO_WINDOW = getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0)
        # detached + no window so closing Maki doesn't kill ollama
        _DETACHED_PROCESS = 0x00000008   # Windows DETACHED_PROCESS flag
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW | _DETACHED_PROCESS,
            close_fds=True,
        )
    except FileNotFoundError:
        print("[Maki] WARNING: `ollama` not found on PATH. Install from "
              "https://ollama.com/download — vision and tool calls will use "
              "cloud fallbacks until then.", flush=True)
        return False
    except Exception as e:
        print(f"[Maki] WARNING: couldn't spawn `ollama serve` ({e}). "
              f"Continuing with cloud fallbacks.", flush=True)
        return False

    # 3. Poll for readiness (up to wait_s seconds)
    for _ in range(wait_s):
        _t.sleep(1)
        try:
            if requests.get(OLLAMA_URL, timeout=2).status_code == 200:
                print("[Maki] Ollama started successfully.", flush=True)
                return True
        except Exception:
            pass

    print(f"[Maki] WARNING: Ollama didn't respond within {wait_s}s — "
          f"vision and tool calls may fail until it comes up.", flush=True)
    return False

_OLLAMA_READY = _ensure_ollama_running()

import config, brain, speech, voice, memory
from gui import MakiWindow
try:
    from wake_unlock_watcher import WakeUnlockWatcher
except ImportError:
    WakeUnlockWatcher = None

_LOG_DIR = "logs"
try:
    import os as _os
    _os.makedirs(_LOG_DIR, exist_ok=True)
    _LOG_FILE = _os.path.join(_LOG_DIR, "startup.log")
    _file_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s"
    ))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(), _file_handler],
    )
except Exception:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
logger = logging.getLogger("main")
logger.info("MAIN_STARTED (pid=%s) — Maki main.py loaded.", _os.getpid())

ACKS = ["Yeah?", "Mm?", "Go on.", "What's up?", "I'm here."]

# Seconds after a wake-only phrase that we accept commands without re-stating wake word
WAKE_ACTIVE_SECONDS = 30

# Strict wake word regex — accepts common STT errors for "maki"
# Includes: maky (y instead of i), mickey/mikey (rhymes, common STT output),
#           marky/markey (sound-alike), mackey/mackie (spelling variants)
# Excludes: bucky, rocky, monkey, sandy (rejected — no phonetic match)
_STRICT_WAKE_RE = re.compile(
    r"^(hey\s+(maki|makey|macky|machi|maky|marky|markey|mackey|mackie|mickey|mikey)|"
    r"okay\s+maki|ok\s+maki|"
    r"maki|makey|macky|machi|maky)\b",
    re.I,
)


def _has_wake(text: str) -> bool:
    """True if text starts with a recognised wake word."""
    return bool(_STRICT_WAKE_RE.match(text.strip()))


def _is_only_wake(text: str) -> bool:
    """True if the phrase is JUST a wake call (nothing meaningful after it)."""
    remainder = _STRICT_WAKE_RE.sub("", text.strip(), count=1).strip(" ,.")
    return _has_wake(text) and not remainder


def _strip_wake(text: str) -> str:
    """Remove the leading wake word so the command reaches the brain."""
    return _STRICT_WAKE_RE.sub("", text.strip(), count=1).strip(" ,.")


class Maki:
    def __init__(self):
        self.window      = MakiWindow()
        self._running    = False
        self._speaking   = threading.Event()
        self._ptt_lock   = threading.Lock()
        self._ptt_stop   = threading.Event()
        self._listen_on  = True
        self._ptt_active = False

        # maxsize=1: always process the freshest command
        self._voice_q = queue.Queue(maxsize=1)

        # Timestamp until which we accept commands without a wake word
        self._wake_active_until = 0.0

        self.window.on_send_text(self._handle_text)
        self.window.on_ptt_start(self._ptt_start)
        self.window.on_ptt_stop(self._ptt_stop_fn)
        self.window.on_pause(self._pause_listen)
        self.window.on_resume(self._resume_listen)
        self.window.on_quit(self._quit)
        # V18 — new GUI callbacks
        self.window.on_think_toggle(self._toggle_think)
        self.window.on_stop(self._stop_now)

    # ── Boot ──────────────────────────────────────────────────────────────────

    def start(self):
        threading.Thread(target=self._boot, daemon=True).start()
        self.window.run()

    def _boot(self):
        self.window.set_status("Starting up…")

        if not speech.mic_available():
            self._speak("I couldn't find a microphone.")
            self.window.set_status("No microphone", color="#ef4444")
        else:
            speech.calibrate(1.2)
            speech.always_on.start()   # persistent stream — no gaps

        mode = brain.check_providers()   # Gemini → Ollama → Basic
        self.window.set_mode(mode)

        if mode == brain.MODE_GEMINI:
            self.window.set_model(config.GEMINI_MODEL)
        elif mode == brain.MODE_OLLAMA:
            actual   = brain._ollama_model_actual or config.OLLAMA_MODEL
            cfg_base = config.OLLAMA_MODEL.split(":")[0]
            act_base = actual.split(":")[0]
            if cfg_base != act_base:
                # Configured model not installed — warn loudly
                warn = (f"⚠️ {config.OLLAMA_MODEL} is NOT installed. "
                        f"Using {actual} as a temporary fallback. "
                        f"Run:  ollama pull {config.OLLAMA_MODEL}")
                logger.warning(warn)
                self.window.add_system_message(warn)
                self.window.set_model(f"{actual} ⚠️ (fallback)")
            else:
                self.window.set_model(actual)
        else:
            self.window.set_model("Basic")

        _hour = time.localtime().tm_hour
        if _hour < 12:
            _greeting = f"Good morning, {config.USER_NAME}. Maki is ready."
        elif _hour < 18:
            _greeting = f"Hey {config.USER_NAME}, Maki is online."
        else:
            _greeting = f"Evening, {config.USER_NAME}. Maki is here."
        # V9: light continuity — acknowledge a prior conversation if one exists
        try:
            _sess = memory.get_last_session_info()
            if _sess.get("total_turns", 0) > 2 and _sess.get("prev_session_end"):
                _gap = time.time() - _sess["prev_session_end"]
                if _gap > 1800:   # more than ~30 min since last turn
                    _greeting += " Good to have you back — I remember where we left off."
        except Exception:
            pass
        self._speak(_greeting)

        # V19 startup banner — REAL ping check (not just key presence).
        # Runs in parallel, typically completes in ~3s when healthy.
        try:
            import startup_check
            _v19_health = startup_check.run_all(parallel=True)
            startup_check.print_banner(_v19_health)
            # Expose to other modules for runtime decisions (skip dead lanes etc.)
            import sys as _sys
            _sys.modules["__main__"].v19_health = _v19_health
        except Exception as _e:
            logger.info("V19 boot check skipped: %s", _e)

        self._running = True
        threading.Thread(target=self._listen_loop,    daemon=True, name="listener").start()
        threading.Thread(target=self._processor_loop, daemon=True, name="processor").start()

        # V7: wake/unlock greeter
        self._watcher = None
        if WakeUnlockWatcher is not None:
            self._watcher = WakeUnlockWatcher(self._on_wake_or_unlock)
            self._watcher.start()

    def _quit(self):
        self._running = False
        self._ptt_stop.set()
        speech.always_on.stop()
        if getattr(self, "_watcher", None):
            try: self._watcher.stop()
            except Exception: pass

    # ── Wake / unlock greeting ────────────────────────────────────────────────
    def _on_wake_or_unlock(self, kind: str):
        """Fired by WakeUnlockWatcher when user returns from lock/sleep."""
        if self._speaking.is_set():
            return  # don't talk over an in-flight reply
        hour = time.localtime().tm_hour
        if kind == "wake":
            msg = f"Welcome back, {config.USER_NAME}."
        elif hour < 12:
            msg = f"Good morning, {config.USER_NAME}."
        elif hour < 18:
            msg = f"Hey {config.USER_NAME}, welcome back."
        else:
            msg = f"Evening, {config.USER_NAME}. I'm here."
        self.window.add_system_message(f"({kind}) {msg}")
        self._speak(msg)

    # ── Listen loop ───────────────────────────────────────────────────────────

    def _listen_loop(self):
        """Reads recognised text from always_on queue. No gaps ever."""
        self.window.set_status("Listening…", color="#10b981")
        while self._running:
            if not self._listen_on or self._ptt_active:
                time.sleep(0.1)
                continue

            text = speech.always_on.get(timeout=0.5)
            if not text:
                continue

            # Keep only the freshest command in the 1-slot queue.
            # V10.2: queue (text, timestamp) so the processor can drop stale ones.
            try:
                self._voice_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._voice_q.put_nowait((text, time.time()))
            except queue.Full:
                pass

        self.window.set_status("Stopped.", color="#555")

    # ── Processor loop ────────────────────────────────────────────────────────

    # Commands older than this when finally reached are discarded — they piled
    # up while Maki was talking and are no longer what the user wants.
    STALE_TRANSCRIPT_SECONDS = 7.0

    def _processor_loop(self):
        """
        Process voice commands serially. V10.2: waits out TTS BEFORE pulling
        from the queue (so it always grabs the freshest transcript, not one
        that's been sitting there), and discards anything that's gone stale.
        """
        while self._running:
            # Wait until Maki is done speaking FIRST — don't hold a transcript
            # across a whole TTS cycle and then act on something 15s old.
            while self._speaking.is_set() and self._running:
                time.sleep(0.05)

            try:
                item = self._voice_q.get(timeout=0.5)
            except queue.Empty:
                continue

            text, ts = item if isinstance(item, tuple) else (item, time.time())
            age = time.time() - ts
            if age > self.STALE_TRANSCRIPT_SECONDS:
                logger.info("Discarded stale transcript (%.1fs old): %r", age, text)
                continue
            # TTS may have started between the wait and the get — re-loop if so.
            if self._speaking.is_set():
                try:
                    self._voice_q.put_nowait((text, ts))   # put it back, retry
                except queue.Full:
                    pass
                continue

            self._handle_voice(text)

    def _handle_voice(self, raw: str):
        self.window.add_system_message(f'🎙 "{raw}"')
        logger.info("Transcript captured: %r", raw)

        now      = time.time()
        has_wake = _has_wake(raw)

        # Wake word heard → extend active window
        if has_wake:
            self._wake_active_until = now + WAKE_ACTIVE_SECONDS
            # Pure wake call (nothing after it) → ack and wait
            if _is_only_wake(raw):
                logger.info("Wake-only phrase — sending ack.")
                self._reply(random.choice(ACKS))
                self.window.set_status("Listening…", color="#10b981")
                return

        # Strip wake prefix so brain sees the clean command
        cmd = _strip_wake(raw) if has_wake else raw
        if not cmd.strip():
            self.window.set_status("Listening…", color="#10b981")
            return

        # Garbled transcript check — ask to repeat (skip if clarification is pending)
        cleaned = brain.clean_transcript(cmd)
        if brain.looks_garbled(cleaned) and not brain.has_pending() and not brain.has_confirm():
            logger.info("Garbled transcript — asking to repeat: %r", cmd)
            self._reply("I might have misheard that. Could you say it again?")
            self.window.set_status("Listening…", color="#10b981")
            return

        logger.info("Processing transcript: %r", cmd)
        self._process(cmd)
        self.window.set_status("Listening…", color="#10b981")

    # ── PTT ───────────────────────────────────────────────────────────────────

    def _ptt_start(self):
        with self._ptt_lock:
            self._ptt_active = True
            self._listen_on  = False
            self._ptt_stop.clear()
            speech.always_on.pause()
            self.window.set_status("Recording… release to send", color="#f87171")
            text = speech.listen_ptt(self._ptt_stop)
            self.window.set_ptt_recording(False)
            speech.always_on.resume()
            self._ptt_active = False
            self._listen_on  = True

        if text:
            self._process(text)
        else:
            self.window.add_system_message("Didn't catch that.")
        self.window.set_status("Listening…", color="#10b981")

    def _ptt_stop_fn(self):
        self._ptt_stop.set()

    # ── Text chat ─────────────────────────────────────────────────────────────

    def _handle_text(self, text: str):
        threading.Thread(target=self._process, args=(text,), daemon=True).start()

    # ── Pause / Resume ────────────────────────────────────────────────────────

    def _pause_listen(self):
        self._listen_on = False
        speech.always_on.pause()
        self.window.set_state("paused")

    def _resume_listen(self):
        self._listen_on = True
        speech.always_on.resume()
        self.window.set_state("listening")

    # ── V18 — Think mode + Stop ───────────────────────────────────────────────

    def _toggle_think(self, enabled: bool):
        """Called when user clicks the GUI 🧠 Think button."""
        import memory
        memory.set_think_mode(bool(enabled))
        logger.info("V18 think-mode → %s", "ON" if enabled else "OFF")

    def _stop_now(self):
        """Called when user clicks the GUI 🛑 Stop button (or says 'stop')."""
        import memory
        memory.request_stop()
        # Halt current TTS immediately
        try:
            voice.stop()
        except Exception as e:
            logger.info("voice.stop() failed: %s", e)
        # Drop any pending transcripts
        try:
            while True: self._voice_q.get_nowait()
        except queue.Empty:
            pass
        # Clear speaking flag so listener returns to idle
        self._speaking.clear()
        self.window.set_state("listening")
        logger.info("V18 STOP signalled by user")

    # ── Core ──────────────────────────────────────────────────────────────────

    def _process(self, text: str):
        self.window.add_user_message(text)
        # Show "clarifying" status if brain has pending
        if brain.has_pending() or brain.has_confirm():
            self.window.set_state("clarify")
        else:
            self.window.set_state("thinking")

        reply = brain.process(text)

        # Update timing / last-tool info strip
        self.window.set_processing_info(brain.get_last_process_ms(), brain.get_last_tool())

        # Update GUI status to show if Maki is now waiting for clarification
        if brain.has_pending():
            self.window.set_state("clarify")
        elif brain.has_confirm():
            self.window.set_state("confirm")

        self._reply(reply or "I'm not sure how to help with that.")

    def _reply(self, text: str):
        # V18: skip TTS if stop is pending (user clicked Stop or said "stop")
        import memory
        if memory.consume_stop():
            logger.info("V18: stop pending — skipping TTS for %r", text[:60])
            self.window.add_maki_message(text)
            return
        logger.info("Response generated: %r", text[:120])
        self.window.add_maki_message(text)
        self.window.set_state("speaking")
        self._speaking.set()
        speech.always_on.set_last_reply(text)   # anti-echo: ignore our own voice
        speech.always_on.mute()                 # stop hearing self during TTS
        logger.info("TTS_START")
        try:
            voice.speak(text)
        finally:
            logger.info("TTS_END")
            time.sleep(0.25)           # echo guard — slightly longer than V7
            speech.always_on.unmute()
            self._speaking.clear()
            # Only return to listening if no pending clarification/confirm
            if not (brain.has_pending() or brain.has_confirm()):
                self.window.set_state("listening")

    def _speak(self, text: str):
        """System speech (boot messages)."""
        logger.info("Maki: %s", text)
        self.window.set_state("speaking")
        self._speaking.set()
        speech.always_on.set_last_reply(text)
        speech.always_on.mute()
        logger.info("TTS_START")
        try:
            voice.speak(text)
        finally:
            logger.info("TTS_END")
            time.sleep(0.25)
            speech.always_on.unmute()
            self._speaking.clear()


# ── Entry ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        Maki().start()
    except KeyboardInterrupt:
        sys.exit(0)
