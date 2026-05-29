"""
voice.py - TTS using edge-tts (Microsoft AriaNeural — natural voice).
Persistent asyncio event loop avoids per-call loop creation overhead.
Falls back to pyttsx3 if edge-tts is unavailable.
"""

import asyncio, ctypes, logging, os, queue, tempfile, threading
import config

logger = logging.getLogger(__name__)

# ── Persistent asyncio event loop (avoids creating/destroying per call) ───────
_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True, name="edge-loop").start()

# ── edge-tts ──────────────────────────────────────────────────────────────────
try:
    import edge_tts as _edge
    _EDGE_OK = True
    logger.info("edge-tts available — AriaNeural voice")
except ImportError:
    _EDGE_OK = False
    logger.info("edge-tts not found — falling back to pyttsx3")

EDGE_VOICE  = getattr(config, "EDGE_VOICE",  "en-US-AriaNeural")
EDGE_RATE   = getattr(config, "EDGE_RATE",   "+5%")
EDGE_VOLUME = getattr(config, "EDGE_VOLUME", "+0%")


# ── MCI MP3 playback (Windows built-in) ──────────────────────────────────────

def _mci(cmd: str):
    buf = ctypes.create_unicode_buffer(512)
    ctypes.windll.winmm.mciSendStringW(cmd, buf, 512, None)
    return buf.value


# V18 — global interrupt flag + active alias for stop()
_stop_evt = threading.Event()
_active_alias = None       # current MCI alias if a playback is in flight
_active_lock = threading.Lock()


def _play_mp3(path: str):
    """V18: poll-based playback so stop() can interrupt cleanly."""
    global _active_alias
    p = path.replace("\\", "/")
    alias = f"maki_tts_{int(time.time()*1000) % 1_000_000}"
    _mci(f'open "{p}" type mpegvideo alias {alias}')
    with _active_lock:
        _active_alias = alias
    try:
        _mci(f"play {alias}")
        # Poll until done OR stop requested
        while True:
            status = _mci(f"status {alias} mode")
            if status != "playing" or _stop_evt.is_set():
                break
            time.sleep(0.05)
    finally:
        try: _mci(f"stop {alias}")
        except Exception: pass
        try: _mci(f"close {alias}")
        except Exception: pass
        with _active_lock:
            _active_alias = None


def stop():
    """V18: interrupt any currently-playing speech immediately."""
    _stop_evt.set()
    # Drain the queue too — anything pending shouldn't play
    while True:
        try:
            item = _q.get_nowait()
            if item is not None:
                _, done_evt = item
                done_evt.set()
            _q.task_done()
        except queue.Empty:
            break
    # Reset flag for next utterance (after a short delay so in-flight stops)
    threading.Timer(0.2, _stop_evt.clear).start()


import time   # used by _play_mp3 polling


# ── edge-tts synthesis ────────────────────────────────────────────────────────

async def _gen(text: str, path: str):
    await _edge.Communicate(text, EDGE_VOICE, rate=EDGE_RATE, volume=EDGE_VOLUME).save(path)


def _speak_edge(text: str):
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.close()
    path = tmp.name
    try:
        # Submit to the persistent loop — no loop create/destroy overhead
        future = asyncio.run_coroutine_threadsafe(_gen(text, path), _loop)
        future.result(timeout=12)
        _play_mp3(path)
    except Exception as exc:
        logger.error("edge-tts failed: %s — falling back to pyttsx3", exc)
        _speak_pyttsx3(text)
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


# ── pyttsx3 fallback ──────────────────────────────────────────────────────────

FEMALE   = ["zira", "hazel", "susan", "eva", "aria", "jenny", "female", "woman"]
_voice_id = None


def _get_voice_id():
    global _voice_id
    if _voice_id:
        return _voice_id
    try:
        import pyttsx3
        e      = pyttsx3.init()
        voices = e.getProperty("voices") or []
        if config.TTS_PREFER_FEMALE:
            for v in voices:
                if any(f in v.name.lower() for f in FEMALE):
                    _voice_id = v.id
                    logger.info("pyttsx3 voice: %s", v.name)
                    break
        if not _voice_id and voices:
            _voice_id = voices[min(config.TTS_VOICE_INDEX, len(voices) - 1)].id
        try:
            e.stop()
        except Exception:
            pass
    except Exception as exc:
        logger.warning("Voice ID discovery failed: %s", exc)
    return _voice_id


def _speak_pyttsx3(text: str):
    try:
        import pyttsx3
        engine = pyttsx3.init()
        engine.setProperty("rate",   config.TTS_RATE)
        engine.setProperty("volume", config.TTS_VOLUME)
        vid = _get_voice_id()
        if vid:
            engine.setProperty("voice", vid)
        engine.say(text)
        engine.runAndWait()
        try:
            engine.stop()
        except Exception:
            pass
    except Exception as exc:
        logger.error("pyttsx3 error: %s", exc)


# ── Worker thread ─────────────────────────────────────────────────────────────

_q     = queue.Queue()
_ready = threading.Event()


def _worker():
    _ready.set()
    while True:
        item = _q.get()
        if item is None:
            break
        text, done_evt = item
        try:
            if _EDGE_OK:
                _speak_edge(text)
            else:
                _speak_pyttsx3(text)
        except Exception as exc:
            logger.error("TTS worker error: %s", exc)
        finally:
            done_evt.set()
            _q.task_done()


_thread = threading.Thread(target=_worker, daemon=True, name="tts-worker")
_thread.start()
_ready.wait(timeout=4)


# ── Public API ────────────────────────────────────────────────────────────────

def speak(text: str):
    """Queue text and block until speech finishes. Safe from any thread."""
    if not text or not text.strip():
        return
    done = threading.Event()
    _q.put((text, done))
    done.wait()


def speak_async(text: str, on_done=None):
    """Fire-and-forget TTS."""
    def _run():
        speak(text)
        if on_done:
            on_done()
    threading.Thread(target=_run, daemon=True).start()
