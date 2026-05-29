"""
speech.py — Continuous VAD microphone listener.

ROOT CAUSE OF "SOMETIMES NOT RESPONDING" (now fixed):
  listen_once had: won.wait(timeout = 2 + 15 + 3 = 20 seconds)
  Workers exited after 2s of silence, but won.wait held for 20 full seconds
  doing NOTHING. That was an 18-second dead window every cycle where any
  speech was completely silently dropped.

FIX: AlwaysOn keeps ONE InputStream open permanently — zero gaps, ever.
  Mute during TTS so Maki doesn't hear her own voice through the mic.
"""

import collections, difflib, logging, os, queue, re, threading, time
import numpy as np
import speech_recognition as sr
import sounddevice as _sd
import config

logger = logging.getLogger(__name__)


def _norm_text(s: str) -> str:
    """Lowercase, strip punctuation/extra space — for transcript comparison."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()

SAMPLE_RATE      = 16_000
BYTES_PER_SAMPLE = 2
CHUNK_SECS       = 0.03      # 30 ms per chunk
PRE_CHUNKS       = 10        # ~300 ms look-back before speech onset
MAX_SILENCE_SECS = getattr(config, "MIC_PAUSE_THRESHOLD",   0.65)  # from config V3
MAX_PHRASE_SECS  = getattr(config, "MIC_PHRASE_TIME_LIMIT", 12.0)  # from config V3

_rec = None


def _get_rec():
    global _rec
    if _rec is None:
        _rec = sr.Recognizer()
        _rec.energy_threshold = 300
    return _rec


def _input_devices() -> list:
    SKIP = ["stereo mix", "pc speaker", "wave out", "sonar", "what u hear",
            "sound mapper", "mapper", "output"]
    out = []
    for i, d in enumerate(_sd.query_devices()):
        if d["max_input_channels"] == 0:               continue
        if d.get("hostapi", 0) == 3:                   continue
        if any(s in d["name"].lower() for s in SKIP):  continue
        out.append((i, int(d.get("default_samplerate", 48_000)), d["name"]))
    return out[:6]


def mic_available() -> bool:
    return bool(_input_devices())


def _resample(a: np.ndarray, fr: int, to: int) -> np.ndarray:
    a = a.flatten()
    if fr == to:
        return a
    n = int(len(a) * to / fr)
    return np.interp(np.linspace(0, 1, n), np.linspace(0, 1, len(a)),
                     a.astype(np.float64)).astype(np.int16)


# ── V9.2 faster-whisper STT backend (local, accurate, offline) ───────────────
_STT_ENGINE         = getattr(config, "STT_ENGINE", "faster-whisper")
_WHISPER_MODEL_NAME = getattr(config, "WHISPER_MODEL", "base.en")
_whisper_model      = None
_whisper_lock       = threading.Lock()
_whisper_failed     = False

# Whisper hallucinates these short phrases on silence/noise — reject them so
# Maki never "hears" a command that was never spoken.
_WHISPER_NOISE = {
    "you", "thank you", "thanks", "thanks for watching", "bye", "uh", "um",
    "so", ".", "...", "", "okay", "thank you for watching", "thanks for watching!",
    "please subscribe", "subscribe",
}


_WHISPER_DEVICE = getattr(config, "WHISPER_DEVICE", "cpu")


def _ensure_cuda_dlls() -> None:
    """
    V10.1: put the pip-installed NVIDIA CUDA DLLs (cuBLAS / cuDNN / nvrtc) on
    the Windows DLL search path. faster-whisper/ctranslate2 need cublas64_12.dll
    at *inference* time — without this, the model loads on the GPU fine but
    every transcribe() throws 'cublas64_12.dll not found'. That was the
    'Maki heard nothing' bug.
    """
    try:
        import site
        roots = list(site.getsitepackages())
        try:
            roots.append(site.getusersitepackages())
        except Exception:
            pass
        added = 0
        for sp in roots:
            nv = os.path.join(sp, "nvidia")
            if not os.path.isdir(nv):
                continue
            for sub in ("cublas", "cudnn", "cuda_nvrtc"):
                d = os.path.join(nv, sub, "bin")
                if os.path.isdir(d):
                    if d not in os.environ.get("PATH", ""):
                        os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
                    try:
                        os.add_dll_directory(d)
                    except Exception:
                        pass
                    added += 1
        if added:
            logger.info("CUDA DLLs: added %d NVIDIA library dir(s) to the search path", added)
    except Exception as e:
        logger.debug("CUDA DLL path setup skipped: %s", e)


def _get_whisper():
    """Lazily load (and cache) the faster-whisper model. None if unavailable.
    V10.1: tries CUDA (RTX GPU) AND verifies it with a real test inference —
    the constructor succeeds even when cuBLAS is missing, so we must actually
    run the model to know CUDA works. Transparently falls back to CPU."""
    global _whisper_model, _whisper_failed
    if _whisper_model is not None or _whisper_failed:
        return _whisper_model
    with _whisper_lock:
        if _whisper_model is not None or _whisper_failed:
            return _whisper_model
        try:
            from faster_whisper import WhisperModel
        except Exception as e:
            logger.warning("faster-whisper not installed (%s) — using Google STT.", e)
            _whisper_failed = True
            return None
        t0 = time.time()
        # Try CUDA first if requested
        if _WHISPER_DEVICE == "cuda":
            _ensure_cuda_dlls()
            try:
                cand = WhisperModel(
                    _WHISPER_MODEL_NAME, device="cuda", compute_type="float16")
                # CRITICAL: validate with a REAL inference. The constructor
                # succeeds even when cuBLAS is missing — only inference fails.
                _seg, _ = cand.transcribe(
                    np.zeros(16000, dtype=np.float32), language="en", beam_size=1)
                list(_seg)                         # force the lazy GPU run
                _whisper_model = cand
                logger.info("faster-whisper '%s' on CUDA (GPU) verified in %.1fs",
                            _WHISPER_MODEL_NAME, time.time() - t0)
                return _whisper_model
            except Exception as cuda_err:
                logger.warning("faster-whisper CUDA unusable (%s) — falling back to CPU.",
                               str(cuda_err)[:140])
        # CPU path (requested, or CUDA failed/verification failed)
        try:
            _whisper_model = WhisperModel(
                _WHISPER_MODEL_NAME, device="cpu", compute_type="int8")
            logger.info("faster-whisper '%s' on CPU loaded in %.1fs",
                        _WHISPER_MODEL_NAME, time.time() - t0)
        except Exception as e:
            logger.warning("faster-whisper unavailable (%s) — using Google STT.", e)
            _whisper_failed = True
    return _whisper_model


def prewarm_stt():
    """Load the Whisper model in the background so the first command isn't slow."""
    if _STT_ENGINE == "faster-whisper":
        threading.Thread(target=_get_whisper, daemon=True, name="whisper-prewarm").start()


# V10.1: ctranslate2 / faster-whisper models are NOT safe for concurrent
# .transcribe() calls. Maki's multi-mic design fires several at once, which
# returned empty/garbage — the "Maki heard nothing" bug. Serialize them.
_whisper_infer_lock = threading.Lock()


# V17 — context-aware STT: pull the last user/assistant turn from perception
# and feed it to Whisper as `initial_prompt`. Biases recognition toward words
# Maki has actually been discussing — fixes "school of chrome" type mishearings
# at the STT stage, before perception even runs.
def _whisper_context_prompt() -> str:
    try:
        import perception
        snap = perception.ctx.snapshot()
        bits = []
        if snap.get("last_user_text"):
            bits.append(f"User: {snap['last_user_text'][:80]}")
        if snap.get("last_assistant"):
            bits.append(f"Maki: {snap['last_assistant'][:80]}")
        if snap.get("window_title"):
            bits.append(f"Window: {snap['window_title'][:60]}")
        if bits:
            # Whisper uses this as a soft language-model hint. Keep it short.
            return " | ".join(bits)
    except Exception:
        pass
    return ""


def _transcribe_whisper(audio: np.ndarray):
    """Local transcription. Returns text, '' for noise, or None if engine failed."""
    m = _get_whisper()
    if m is None:
        return None
    try:
        f32 = audio.astype(np.float32) / 32768.0          # int16 → float32 [-1,1]
        # V17: feed recent context as Whisper's initial_prompt for better
        # word recognition on follow-ups and ambiguous utterances.
        init_prompt = _whisper_context_prompt() or None
        with _whisper_infer_lock:
            segments, _info = m.transcribe(
                f32, language="en", beam_size=1,
                vad_filter=True, condition_on_previous_text=False,
                initial_prompt=init_prompt,
            )
            text = " ".join(s.text for s in segments)
        text = re.sub(r"\s+", " ", text).strip().strip(".,!?").lower()
        if not text or text in _WHISPER_NOISE:
            return ""
        return text
    except Exception as e:
        logger.warning("faster-whisper transcribe error: %s", e)
        return None


def _transcribe_google(audio: np.ndarray) -> str:
    obj = sr.AudioData(audio.tobytes(), SAMPLE_RATE, BYTES_PER_SAMPLE)
    try:
        return _get_rec().recognize_google(obj).lower().strip()
    except sr.UnknownValueError:
        return ""
    except sr.RequestError as e:
        logger.warning("Google STT request error: %s", e)
        return ""
    except Exception as e:
        logger.debug("Google STT: %s", e)
        return ""


def _transcribe_groq(audio: np.ndarray) -> str | None:
    """V19 Step 3: Groq Whisper Large v3 Turbo (~200ms, cloud).
    Returns text, '' for noise, or None if engine unavailable/failed (so the
    caller falls through to local faster-whisper).

    VERIFY: Groq's free tier — whisper-large-v3-turbo and llama-3.1-8b-instant
    are billed against SEPARATE quota buckets (audio-seconds vs text-tokens),
    confirmed at console.groq.com/settings/limits as of 2026-05-20. If this
    changes, set budget.GROQ_WHISPER_SHARES_TEXT_QUOTA = True (no other
    code change needed)."""
    try:
        import groq_lane
    except Exception:
        return None
    if not groq_lane.available():
        return None
    # V19 Step 3: +300ms trailing audio buffer to capture sentence tails.
    # The mic listener already includes a small tail in its capture, but VAD
    # can clip 100-200ms. We pad here so Groq doesn't see truncated audio.
    tail_samples = int(0.3 * SAMPLE_RATE)
    padded = np.concatenate([audio, np.zeros(tail_samples, dtype=audio.dtype)])
    return groq_lane.transcribe_pcm(padded, sample_rate=SAMPLE_RATE)


def _transcribe(audio: np.ndarray) -> str:
    """V19: Groq Whisper Turbo (cloud, ~200ms) → faster-whisper (local) → Google STT.

    V19 logs which engine actually handled the call so you can grep the
    log to confirm Groq is being used (vs silently falling back to local)."""
    import time as _t
    t0 = _t.time()

    # 1. Groq Whisper Turbo — fastest if network + quota OK
    g = _transcribe_groq(audio)
    if g is not None:
        logger.info("STT: groq_whisper -> %r (%dms)",
                    (g[:60] if g else "<empty>"), int((_t.time()-t0)*1000))
        return g

    # 2. Local faster-whisper — accurate offline fallback (V18 default)
    if _STT_ENGINE == "faster-whisper":
        result = _transcribe_whisper(audio)
        if result is not None:
            logger.info("STT: faster_whisper(local) -> %r (%dms)",
                        (result[:60] if result else "<empty>"), int((_t.time()-t0)*1000))
            return result

    # 3. Google STT — last resort
    g_out = _transcribe_google(audio)
    logger.info("STT: google -> %r (%dms)",
                (g_out[:60] if g_out else "<empty>"), int((_t.time()-t0)*1000))
    return g_out


# Device chosen by calibrate() — (idx, rate, name). None until calibrated.
_best_device = None


def calibrate(duration: float = 1.2):
    """
    Measure every input device, then SELECT the one that actually produces
    signal (a dead/unplugged mic reads ~0; any live mic reads > 0).
    Stores the winner in _best_device so the always-on stream opens THAT one
    instead of blindly grabbing device index 0.
    """
    global _best_device
    devices  = _input_devices()
    measured = []   # (rms, idx, rate, name)
    for idx, rate, name in devices:
        try:
            f = _sd.rec(int(rate * duration), samplerate=rate, channels=1,
                        dtype="int16", device=idx, blocking=True)
            rms = int(np.sqrt(np.mean(f.astype(np.float32) ** 2)))
            logger.info("Calibrate [%d] %-40s rms=%d", idx, name[:40], rms)
            measured.append((rms, idx, rate, name))
        except Exception as e:
            logger.debug("Calibrate [%d] failed: %s", idx, e)

    if not measured:
        logger.error("Calibrate: no input devices could be measured.")
        return

    # Pick the live device with the strongest signal. Dead mics read ~0,
    # so 'max rms' reliably avoids unplugged/muted devices.
    live   = [m for m in measured if m[0] > 5]
    chosen = max(live or measured, key=lambda m: m[0])
    chosen_rms, c_idx, c_rate, c_name = chosen
    _best_device = (c_idx, c_rate, c_name)
    logger.info("Calibrate: SELECTED [%d] %s (rms=%d)", c_idx, c_name[:40], chosen_rms)
    if not live:
        logger.warning("Calibrate: every mic read near-silent — selection may be unreliable.")

    # Re-measure the chosen device alone for a clean noise-floor reading,
    # then set the VAD threshold just above its floor.
    try:
        f = _sd.rec(int(c_rate * 0.8), samplerate=c_rate, channels=1,
                    dtype="int16", device=c_idx, blocking=True)
        floor = int(np.sqrt(np.mean(f.astype(np.float32) ** 2)))
    except Exception:
        floor = chosen_rms
    threshold = max(250, min(3000, int(floor * 2.5) + 250))
    _get_rec().energy_threshold = threshold
    always_on.thr_energy        = threshold
    logger.info("VAD threshold=%d  (chosen device floor=%d)", threshold, floor)


# ── Continuous always-on listener ─────────────────────────────────────────────

class _AlwaysOnListener:
    """
    Keeps one mic stream open permanently — no gaps between phrases.
    mute() before TTS / unmute() after to suppress echo pickup.
    pause() / resume() for the GUI pause button.
    """

    def __init__(self):
        self._text_q    = queue.Queue()
        self._running   = False
        self._muted     = False
        self._paused    = False
        self.thr_energy = 300     # updated by calibrate()
        # Dedup: two mics hearing the same phrase shouldn't double-fire
        self._last_text      = ""
        self._last_text_time = 0.0
        self._dedup_lock     = threading.Lock()
        # V19 BUG-1 FIX: STT arbitration. With multiple mics, every mic that
        # detected speech-end was spawning its own _tx → triple Groq quota
        # and 3 parallel Whisper calls per utterance. Now the FIRST mic to
        # call _tx claims a 1.2s window; others within that window skip the
        # transcription entirely. The dedup layer below still catches any
        # near-duplicate transcripts that slip through.
        self._stt_claim_lock     = threading.Lock()
        self._stt_claimed_until  = 0.0   # time.monotonic() seconds
        # Anti-echo: normalized text of Maki's last spoken reply
        self._last_reply     = ""
        self._last_reply_time = 0.0

    def set_last_reply(self, text: str):
        """Called by main.py before TTS so we can ignore our own voice echo."""
        with self._dedup_lock:
            self._last_reply      = _norm_text(text)
            self._last_reply_time = time.monotonic()

    def start(self):
        if self._running:
            return
        self._running = True
        logger.info("LISTEN_START")
        prewarm_stt()   # V9.2: load Whisper in the background — first command stays fast
        threading.Thread(target=self._supervisor, daemon=True, name="always-on").start()

    def stop(self):
        self._running = False
        logger.info("LISTEN_STOP")

    def mute(self):
        # V18: keep listening, but flag for "in TTS" so barge-in detector engages
        self._muted = True
    def unmute(self): self._muted  = False
    def pause(self):  self._paused = True
    def resume(self): self._paused = False
    def is_muted(self): return self._muted

    # V18 — Barge-in interrupt detection
    def signal_user_interrupt(self):
        """Called by barge-in detector when user speech is heard during TTS.
        Halts current TTS and clears queues."""
        try:
            import voice, memory
            voice.stop()
            memory.request_stop()
            logger.info("V18 BARGE-IN: user interrupted TTS")
        except Exception as e:
            logger.info("barge-in signal failed: %s", e)

    def get(self, timeout: float = 0.5) -> str:
        """Return next recognised phrase, or '' if nothing within timeout."""
        try:
            return self._text_q.get(timeout=timeout)
        except queue.Empty:
            return ""

    def _supervisor(self):
        """
        Monitor EVERY input device at once — one VAD thread per mic.
        Whichever mic you actually speak into crosses its own threshold and
        transcribes; dead/silent mics never trigger. This means the active
        mic is picked up automatically, with no fragile startup guess.
        """
        devices = _input_devices()
        if not devices:
            logger.error("No microphone found — listener cannot start.")
            return
        logger.info("AlwaysOn: monitoring %d input device(s) simultaneously: %s",
                    len(devices), ", ".join(f"[{i}]" for i, _, _ in devices))
        for idx, rate, name in devices:
            threading.Thread(
                target=self._device_loop, args=(idx, rate, name),
                daemon=True, name=f"mic-{idx}",
            ).start()

    def _device_loop(self, idx: int, rate: int, name: str):
        """
        Run one device's VAD stream, auto-restarting it on error.
        V10.1: never permanently gives up — after a burst of quick failures it
        backs off longer and keeps trying, so listening always recovers (the
        old version killed a device after 5 fails — if a mic hiccuped, it
        stayed dead and Maki went deaf on that device).
        """
        fails = 0
        while self._running:
            try:
                self._stream(idx, rate, name)
                fails = 0   # a clean run resets the counter
            except Exception as e:
                fails += 1
                backoff = 1.5 if fails < 5 else 20.0   # back off after a burst
                logger.warning("AlwaysOn [%d] %s error: %s (fail %d, retry in %.0fs)",
                               idx, name[:30], e, fails, backoff)
                time.sleep(backoff)
        logger.info("AlwaysOn [%d] %s: stopped (listener shut down).", idx, name[:30])

    def _stream(self, idx: int, rate: int, name: str):
        cs         = max(64, int(rate * CHUNK_SECS))
        max_sil    = max(3, int(MAX_SILENCE_SECS / CHUNK_SECS))
        max_phrase = int(MAX_PHRASE_SECS / CHUNK_SECS)

        pre        = collections.deque(maxlen=PRE_CHUNKS)
        buf        = []
        heard      = False
        sil_cnt    = 0
        phrase_cnt = 0

        logger.info("AlwaysOn: opening [%d] %s @ %d Hz", idx, name[:40], rate)
        with _sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                              blocksize=cs, device=idx) as stream:
            # Per-device self-calibration: measure THIS mic's own noise floor.
            floor_samples = []
            for _ in range(int(0.5 / CHUNK_SECS)):
                if not self._running:
                    return
                c, _ = stream.read(cs)
                floor_samples.append(
                    float(np.sqrt(np.mean(c.flatten().astype(np.float32) ** 2)))
                )
            floor = (sorted(floor_samples)[len(floor_samples) // 2]
                     if floor_samples else 0.0)
            thr = max(180, min(3000, int(floor * 2.5) + 200))
            logger.info("AlwaysOn [%d] %s: floor=%.0f threshold=%d — listening",
                        idx, name[:30], floor, thr)

            while self._running:
                chunk, _ = stream.read(cs)
                flat = chunk.flatten()
                rms  = float(np.sqrt(np.mean(flat.astype(np.float32) ** 2)))

                # Paused: drain silently, reset state
                if self._paused:
                    pre.clear()
                    if heard:
                        buf.clear()
                        heard      = False
                        sil_cnt    = 0
                        phrase_cnt = 0
                    continue

                # V18 — Muted (during TTS): instead of silently draining,
                # check for barge-in. Higher threshold to avoid TTS echo.
                # If we see SUSTAINED loud audio while Maki is speaking,
                # treat it as user wanting to interrupt.
                if self._muted:
                    pre.clear()
                    # Barge-in threshold: 2x normal threshold (Maki's echo
                    # is real but typically below this on a primary mic)
                    barge_thr = thr * 2
                    if rms >= barge_thr:
                        self._barge_cnt = getattr(self, "_barge_cnt", 0) + 1
                        # Require ~250ms of sustained loud audio (8 chunks @ 30ms)
                        if self._barge_cnt >= 8:
                            self.signal_user_interrupt()
                            self._barge_cnt = 0
                    else:
                        self._barge_cnt = 0
                    # Reset state — we don't process audio during TTS,
                    # next utterance starts fresh after interrupt
                    if heard:
                        buf.clear(); heard = False
                        sil_cnt = 0; phrase_cnt = 0
                    continue

                if not heard:
                    pre.append(flat.copy())
                    if rms >= thr:
                        heard = True
                        buf.extend(pre)          # include pre-speech buffer
                        buf.append(flat.copy())
                        sil_cnt    = 0
                        phrase_cnt = len(buf)
                else:
                    buf.append(flat.copy())
                    phrase_cnt += 1
                    sil_cnt = 0 if rms >= thr else sil_cnt + 1

                    # V17 LITE smart-turn: if the user has only been speaking
                    # briefly (< 0.8s of actual heard audio), give them more
                    # silence headroom — they're probably mid-thought (e.g.
                    # "open chrome and... uh... go to youtube"). This is a
                    # cheap heuristic version of pipecat smart-turn-v2 that
                    # doesn't need a separate ML model.
                    actual_speech_secs = (phrase_cnt - sil_cnt) * CHUNK_SECS
                    adaptive_max_sil = max_sil
                    if actual_speech_secs < 0.8:
                        adaptive_max_sil = int(max_sil * 1.6)   # +60% silence headroom
                    elif actual_speech_secs > 3.5:
                        adaptive_max_sil = max(2, int(max_sil * 0.85))  # quicker fire on long speech

                    if sil_cnt >= adaptive_max_sil or phrase_cnt >= max_phrase:
                        audio = np.concatenate(buf).flatten()
                        if rate != SAMPLE_RATE:
                            audio = _resample(audio, rate, SAMPLE_RATE)
                        # Transcribe off-thread — VAD never stalls
                        threading.Thread(
                            target=self._tx, args=(audio.copy(), name),
                            daemon=True
                        ).start()
                        pre.clear()
                        buf.clear()
                        heard      = False
                        sil_cnt    = 0
                        phrase_cnt = 0

    def _tx(self, audio: np.ndarray, src: str = ""):
        # V19 BUG-1 FIX: pre-STT arbitration. First mic to enter wins the
        # 1.2s claim window; later mics for the same utterance skip the
        # network call entirely. Stops the 3x Groq Whisper firings per
        # utterance that triple-charged the daily quota.
        now_m = time.monotonic()
        with self._stt_claim_lock:
            if now_m < self._stt_claimed_until:
                logger.debug("STT arbitration: %s skipped (another mic transcribing)",
                             src[:24])
                return
            self._stt_claimed_until = now_m + 1.2

        text = _transcribe(audio)
        if not text:
            # V10.1: make "spoke but heard nothing" visible (not silent)
            dur = len(audio) / SAMPLE_RATE
            if dur > 0.7:
                logger.info("STT produced no text for %.1fs of audio (%s)",
                            dur, src[:24])
            return
        norm = _norm_text(text)
        if not norm:
            return
        now = time.monotonic()

        with self._dedup_lock:
            # ── Anti-echo: ignore transcripts that match Maki's last reply ───
            lr      = self._last_reply
            lr_age  = now - self._last_reply_time
            if lr and lr_age < 8.0 and (
                norm == lr or norm in lr or lr in norm or _similar(norm, lr) > 0.72
            ):
                logger.info("SELF_ECHO_IGNORED: %r", text)
                return

            # ── Dedup: two mics hear the same phrase ~ms apart; STT may vary ─
            recent = (now - self._last_text_time) < 2.5
            prev   = self._last_text
            prev_n = _norm_text(prev)
            if recent and prev_n and (
                norm == prev_n or norm in prev_n or prev_n in norm
                or _similar(norm, prev_n) > 0.82
            ):
                logger.info("DUPLICATE_TRANSCRIPT_IGNORED: %r (from %s)", text, src[:20])
                self._last_text_time = now   # extend window so 3rd dupe also caught
                return
            self._last_text      = text
            self._last_text_time = now

        # ── Low-confidence garbage filter — single weird non-word token ─────
        words = norm.split()
        if len(words) == 1 and len(words[0]) <= 2:
            logger.info("LOW_CONFIDENCE_TRANSCRIPT: %r", text)
            return

        logger.info("Heard [%s]: %s", src[:24], text)
        self._text_q.put(text)


always_on = _AlwaysOnListener()


# ── PTT (push-to-talk) ────────────────────────────────────────────────────────

def listen_ptt(stop_evt: threading.Event) -> str:
    devices = _input_devices()
    if not devices:
        return ""
    ordered   = devices[:3]
    chunks_by = {idx: [] for idx, _, _ in ordered}

    def rec(idx, rate):
        cs = max(64, int(rate * CHUNK_SECS))
        try:
            with _sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                                  blocksize=cs, device=idx) as s:
                while not stop_evt.is_set():
                    c, _ = s.read(cs)
                    chunks_by[idx].append(c.flatten().copy())
        except Exception as e:
            logger.debug("PTT [%d]: %s", idx, e)

    threads = [threading.Thread(target=rec, args=(i, r), daemon=True)
               for i, r, _ in ordered]
    for t in threads:
        t.start()
    stop_evt.wait()
    time.sleep(0.15)

    best, best_peak = None, 0
    for idx, rate, _ in ordered:
        ch = chunks_by[idx]
        if not ch:
            continue
        audio = np.concatenate(ch).flatten()
        if rate != SAMPLE_RATE:
            audio = _resample(audio, rate, SAMPLE_RATE)
        peak = int(np.max(np.abs(audio.astype(np.float32))))
        if peak > best_peak:
            best_peak, best = peak, audio

    if best is None or best_peak < 30:
        return ""
    return _transcribe(best)
