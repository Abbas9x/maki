"""
groq_lane.py — V19 Step 3

Groq cloud provider for two lanes:
  - chat:    llama-3.1-8b-instant     (casual chat lane, 14,400 RPD / 500K TPD)
  - whisper: whisper-large-v3-turbo   (STT, ~200ms latency)

Both share one API key (GROQ_API_KEY). The free-tier rate-limit page treats
chat tokens and whisper audio-seconds as INDEPENDENT counters as of the build
date — see VERIFY note in budget.py. If that ever flips to shared quota,
set budget.GROQ_WHISPER_SHARES_TEXT_QUOTA = True (no other changes needed).

Functions return "" / None on any failure so callers can fall through cleanly.
"""

from __future__ import annotations
import io, logging, os, time, wave
import requests

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_CHAT_URL    = "https://api.groq.com/openai/v1/chat/completions"
GROQ_WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

GROQ_CHAT_MODEL    = os.getenv("GROQ_CHAT_MODEL",    "llama-3.1-8b-instant")
GROQ_WHISPER_MODEL = os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")


def available() -> bool:
    return bool(GROQ_API_KEY)


# ── Chat ─────────────────────────────────────────────────────────────────────
def chat(messages: list[dict], max_tokens: int = 300, temperature: float = 0.6,
         timeout: float = 8.0) -> str:
    """OpenAI-compatible chat completion via Groq. Returns reply text or ''."""
    if not GROQ_API_KEY:
        return ""
    # Quota guard
    try:
        from budget import groq_chat_available, groq_chat_record, count_messages
        ok, reason = groq_chat_available(est_tokens=max_tokens + count_messages(messages))
        if not ok:
            logger.info("groq_lane.chat: quota guard blocked — %s", reason)
            return ""
    except Exception:
        pass

    # Breadcrumb
    try:
        from breadcrumb import trail
    except Exception:
        from contextlib import nullcontext as trail
        def _t(*a, **kw): return trail()
        trail = _t   # type: ignore

    with trail("GROQ_CHAT", "completions", model=GROQ_CHAT_MODEL):
        try:
            r = requests.post(
                GROQ_CHAT_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": GROQ_CHAT_MODEL, "messages": messages,
                      "max_tokens": max_tokens, "temperature": temperature,
                      "stream": False},
                timeout=timeout,
            )
            if r.status_code != 200:
                logger.info("Groq chat HTTP %d: %s", r.status_code, r.text[:120])
                return ""
            data = r.json()
            reply = (data.get("choices", [{}])[0]
                         .get("message", {}).get("content", "") or "").strip()
            usage = data.get("usage", {}) or {}
            tokens = int(usage.get("total_tokens", 0)) or (max_tokens + 50)
            try:
                from budget import groq_chat_record
                groq_chat_record(req_count=1, tokens_used=tokens)
            except Exception:
                pass
            return reply
        except Exception as e:
            logger.info("Groq chat error: %s", e)
            return ""


# ── Whisper STT ──────────────────────────────────────────────────────────────
def transcribe_pcm(audio_int16, sample_rate: int = 16000,
                   language: str = "en", timeout: float = 8.0) -> str | None:
    """
    Transcribe a numpy int16 PCM buffer via Groq Whisper. Returns transcript
    string, '' for noise/empty, or None on engine failure (so the caller
    falls back to local faster-whisper).
    """
    if not GROQ_API_KEY:
        return None
    if audio_int16 is None or len(audio_int16) == 0:
        return ""

    duration_s = len(audio_int16) / float(sample_rate)
    # Quota guard
    try:
        from budget import groq_whisper_available, groq_whisper_record
        ok, reason = groq_whisper_available(est_audio_seconds=duration_s)
        if not ok:
            logger.info("groq_lane.transcribe: quota blocked — %s", reason)
            return None   # fall back to local
    except Exception:
        pass

    # Wrap PCM in a WAV container in-memory (Groq expects a file upload)
    buf = io.BytesIO()
    try:
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)   # int16
            wf.setframerate(sample_rate)
            wf.writeframes(audio_int16.tobytes())
    except Exception as e:
        logger.info("groq_lane.transcribe: wav-encode failed: %s", e)
        return None
    buf.seek(0)

    # Breadcrumb
    try:
        from breadcrumb import trail
    except Exception:
        from contextlib import nullcontext as trail
        def _t(*a, **kw): return trail()
        trail = _t   # type: ignore

    with trail("GROQ_WHISPER", "transcribe", duration_s=round(duration_s, 2)):
        try:
            r = requests.post(
                GROQ_WHISPER_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": ("audio.wav", buf, "audio/wav")},
                data={"model": GROQ_WHISPER_MODEL, "language": language,
                      "response_format": "text", "temperature": "0"},
                timeout=timeout,
            )
            if r.status_code != 200:
                logger.info("Groq Whisper HTTP %d: %s", r.status_code, r.text[:120])
                return None
            text = (r.text or "").strip()
            try:
                from budget import groq_whisper_record
                groq_whisper_record(audio_seconds=duration_s)
            except Exception:
                pass
            return text.lower().strip().strip(".,!?")
        except Exception as e:
            logger.info("Groq Whisper error: %s", e)
            return None
