"""
vision_tools.py — V13 Maki Vision: see the screen and reason about it.

Strategy (100% free):
  1. Capture full screen with PIL.ImageGrab (already proven in screenshot_tools)
  2. Downscale to a reasonable size (vision models choke on huge images;
     also keeps the request payload small).
  3. Send to qwen2.5vl:7b via Ollama /api/chat (LOCAL, free, GPU-accelerated).
     - qwen2.5vl has native UI grounding — it can return bbox coordinates
       for "click X" and "the search box at top" style requests.
  4. Cloud fallback: Gemini 2.5 Flash multimodal (free tier) if Ollama vision
     is missing or returns an empty/error response.

Public API (used by agent.py):
  look_at_screen(question)              -> str       (Maki's natural answer)
  describe_screen()                     -> str       (free-form summary)
  find_on_screen(target)                -> dict      ({label, bbox, found})
  read_text_on_screen()                 -> str       (best-effort OCR via VLM)
  vision_provider_status()              -> dict
"""

from __future__ import annotations
import base64, io, logging, os, threading, time
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
OLLAMA_URL          = getattr(config, "OLLAMA_URL", "http://localhost:11434/api/chat")
VISION_MODEL_OLLAMA = os.getenv("VISION_MODEL", "qwen3-vl:4b")   # V19: 2b → 4b (GUI-agent-trained, +1GB VRAM)
VISION_KEEP_ALIVE   = os.getenv("VISION_KEEP_ALIVE", "30m")
VISION_TIMEOUT      = int(os.getenv("VISION_TIMEOUT", "45"))   # V14.5: faster failure
MAX_DIMENSION       = 1024   # V14.5: smaller image = 30% faster inference

_capture_lock = threading.Lock()   # serialize captures across threads

# V14.5: screenshot cache. Drops chained vision cost from N captures to 1.
# Cache for 4 seconds — long enough to chain ("click X" + "click Y") but
# short enough that UI changes don't get stale answers.
_CACHE_TTL    = 4.0
_cache_lock   = threading.Lock()
_cached_b64   = None
_cached_at    = 0.0


# ── Capture ─────────────────────────────────────────────────────────────────
# V14.1: prefer `mss` over PIL.ImageGrab — ImageGrab.grab(all_screens=True)
# can SIGSEGV on multi-monitor Windows under load (the V14 crash).
# V17: each path wrapped in defensive try/finally to release native handles
# even on partial failures (suspected cause of repeated SIGSEGVs).
# Order: mss (primary monitor only) → PIL primary → pyautogui.
def _capture_full_screen():
    """Returns a PIL.Image of the full desktop, or None. Crash-hardened."""
    # 1. mss — most stable on Windows. V17: capture PRIMARY monitor only
    # (monitor[1], not monitor[0] which is virtual all-monitors bbox) to
    # avoid the multi-monitor SIGSEGV path entirely.
    sct = None
    try:
        import mss
        from PIL import Image
        sct = mss.mss()
        # monitor[1] is primary; monitor[0] is "all monitors" virtual
        mons = sct.monitors
        mon = mons[1] if len(mons) > 1 else mons[0]
        raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.rgb)
        return img
    except Exception as e:
        logger.warning("mss capture failed: %s — trying PIL", e)
    finally:
        if sct is not None:
            try: sct.close()
            except Exception: pass
    # 2. PIL primary monitor only
    try:
        from PIL import ImageGrab
        return ImageGrab.grab()
    except Exception as e:
        logger.warning("ImageGrab failed: %s — trying pyautogui", e)
    # 3. pyautogui last resort
    try:
        import pyautogui
        return pyautogui.screenshot()
    except Exception as e:
        logger.error("All capture paths failed: %s", e)
        return None


def _downscale(img, max_side: int = MAX_DIMENSION):
    """Shrink so the longest side <= max_side, keep aspect."""
    w, h = img.size
    longest = max(w, h)
    if longest <= max_side:
        return img
    scale = max_side / longest
    new_size = (int(w * scale), int(h * scale))
    try:
        from PIL import Image
        return img.resize(new_size, Image.LANCZOS)
    except Exception:
        return img.resize(new_size)


def _img_to_b64(img, fmt: str = "PNG") -> str:
    buf = io.BytesIO()
    img.save(buf, fmt)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _capture_b64(force_fresh: bool = False) -> Optional[str]:
    """V14.5: returns cached capture if <CACHE_TTL old, else fresh.
    Pass force_fresh=True to invalidate after an action that changed the screen."""
    import time as _t
    global _cached_b64, _cached_at
    if not force_fresh:
        with _cache_lock:
            if _cached_b64 and (_t.time() - _cached_at) < _CACHE_TTL:
                return _cached_b64
    with _capture_lock:
        img = _capture_full_screen()
    if img is None:
        return None
    img = _downscale(img)
    try:
        from PIL import Image
        if img.mode != "RGB":
            img = img.convert("RGB")
    except Exception:
        pass
    b64 = _img_to_b64(img, fmt="JPEG")
    with _cache_lock:
        _cached_b64 = b64
        _cached_at  = _t.time()
    return b64


def invalidate_cache() -> None:
    """Call after any action that changed the screen (click, scroll, type)."""
    global _cached_b64, _cached_at
    with _cache_lock:
        _cached_b64 = None
        _cached_at  = 0.0


# ── VRAM management: evict the chat model before EVERY vision call ──────────
# V19 BUG-A FIX: On 8 GB RTX 4060 Laptop, qwen3-vl:4b (3.3 GB) + hermes3:8b
# (4.7 GB) = ~8 GB → entire card. If hermes3 is loaded from a tool call,
# qwen3-vl:4b can't fit and Ollama returns empty/short text (no clean error),
# which Maki then reports as "vision failed". The lock prevents *concurrent*
# load but doesn't prevent *sequential* OOM when both models are warm.
#
# Fix: force-evict hermes3 BEFORE every vision call so qwen3-vl:4b has the
# whole card. Hermes3 reloads on the next tool call (~3-5s cold-start, but
# correct behavior).
def _evict_chat_model() -> None:
    """Force Ollama to unload hermes3 (or whatever OLLAMA_MODEL is). Uses
    /api/generate with keep_alive=0 — this is the documented way to tell
    Ollama 'drop this model now'."""
    try:
        chat_model = config.OLLAMA_MODEL
    except Exception:
        chat_model = "hermes3:8b"
    try:
        # /api/generate with keep_alive:0 unloads the model. No prompt sent.
        requests.post(
            "http://localhost:11434/api/generate",
            json={"model": chat_model, "keep_alive": 0},
            timeout=4,
        )
        logger.info("Vision: evicted '%s' from VRAM (freeing for qwen3-vl)", chat_model)
    except Exception as e:
        logger.debug("Evict chat model failed (non-fatal): %s", str(e)[:80])


def _vram_check_at_import():
    """Warn at module-load if VRAM is tight."""
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi","--query-gpu=memory.free","--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        free_mib = int((out.stdout or "0").strip().split("\n")[0])
        free_gb = free_mib / 1024.0
        if free_gb < 4.0:
            logger.warning("VRAM TIGHT: %.1f GB free — vision and tool calls will "
                           "take turns loading. Expect 5-10s on first call after "
                           "switching between qwen3-vl:4b and hermes3:8b.", free_gb)
        else:
            logger.info("VRAM at boot: %.1f GB free.", free_gb)
    except Exception:
        pass

_vram_check_at_import()


# ── Ollama vision call (qwen3-vl) ───────────────────────────────────────────
def _ollama_vision(prompt: str, b64_img: str, timeout: int = VISION_TIMEOUT) -> str:
    """Call qwen2.5vl via Ollama /api/chat. Returns the text reply or ''.
    Will retry once after evicting the chat model if VRAM is exhausted."""
    def _call(timeout_s: int) -> requests.Response:
        return requests.post(
            OLLAMA_URL,
            json={
                "model": VISION_MODEL_OLLAMA,
                "messages": [{
                    "role":   "user",
                    "content": prompt,
                    "images": [b64_img],
                }],
                "stream":     False,
                "keep_alive": VISION_KEEP_ALIVE,
                # Shrink context so model fits in 8GB VRAM (default 4096 forces CPU)
                "options": {"num_ctx": 2048, "num_predict": 350},
            },
            timeout=timeout_s,
        )

    # V19 Step 1.5: instrument vision calls for regression attribution.
    try:
        from breadcrumb import trail
    except Exception:
        from contextlib import nullcontext as trail   # safe no-op
        def _no_trail(*a, **kw): return trail()
        trail = _no_trail   # type: ignore

    # V19 Step 2.5: serialize local Ollama models on 8 GB VRAM (qwen3-vl:4b
    # + hermes3:8b together = 8.5 GB > card capacity).
    from local_lock import local_model_slot

    # V19 BUG-1b FIX: Removed manual eviction call. OLLAMA_MAX_LOADED_MODELS=1
    # (set in main.py at startup) tells Ollama to auto-swap models on the 8 GB
    # card. The manual /api/generate keep_alive=0 path raced under sustained
    # use — Ollama's own model-manager is the reliable mechanism.

    with local_model_slot("vision"), trail("VISION", "ollama_call", model=VISION_MODEL_OLLAMA):
        try:
            r = _call(timeout)
            # If 500 with "more system memory" → evict chat model and retry once
            if r.status_code == 500 and "memory" in r.text.lower():
                logger.info("Vision: VRAM full, evicting chat model and retrying")
                _evict_chat_model()
                time.sleep(1.0)
                r = _call(timeout + 30)   # cold-load takes longer
            r.raise_for_status()
            msg = r.json().get("message", {}) or {}
            return (msg.get("content") or "").strip()
        except requests.Timeout:
            logger.info("Vision: Ollama %s timed out", VISION_MODEL_OLLAMA)
            return ""
        except Exception as e:
            logger.info("Vision: Ollama failed: %s", str(e)[:140])
            return ""


# ── Gemini vision fallback (free-tier multimodal) ───────────────────────────
def _gemini_vision(prompt: str, b64_img: str) -> str:
    """Use Gemini Flash (already configured in brain) as cloud vision backup."""
    try:
        import brain
        # Ensure Gemini was initialized (no-op once cached)
        if not getattr(brain, "_gemini_ok", False):
            try: brain.check_gemini()
            except Exception: pass
        if not brain._can_use_gemini():
            return ""
        from google.genai import types
        client = brain._get_genai_client()
        img_bytes = base64.b64decode(b64_img)
        # Use Part.from_bytes for inline image
        parts = [
            types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
            types.Part.from_text(text=prompt),
        ]
        cfg = types.GenerateContentConfig(temperature=0.4, max_output_tokens=500)
        resp = client.models.generate_content(
            model=config.GEMINI_MODEL, contents=parts, config=cfg,
        )
        return (resp.text or "").strip()
    except Exception as e:
        try:
            import brain
            brain._handle_gemini_error(e)
        except Exception:
            pass
        # V19 BUG-E FIX: log the exception TYPE + full message so we can tell
        # whether Gemini failed due to quota / key / network / API change /
        # rate-limit. Previously "failed: ..." was truncated to 140 chars
        # and hid the type.
        logger.error("Vision: Gemini fallback failed [%s]: %s",
                     type(e).__name__, str(e)[:400])
        return ""


# ── Internal: ask the vision model with auto-fallback ───────────────────────
_VISION_SYSTEM = (
    "You are a screen-reading assistant. You will be shown a screenshot of a "
    "Windows PC desktop. Look at it CAREFULLY and answer the user's question "
    "about what is actually on screen. Do not guess or make things up — only "
    "describe what you can actually see. If you can't see the answer, say so. "
    "Keep replies concise and voice-friendly (1-3 sentences) unless the user "
    "asks for detail."
)


def _ask_vision(question: str, b64_img: str) -> str:
    """V19: Try local qwen3-vl:4b FIRST (GUI-agent trained, fits 8GB VRAM),
    fall back to Gemini Flash only if local is unavailable or empty.

    V19 reversed V17's order — V17 used Gemini first which meant qwen3-vl
    rarely ran and the "headline vision upgrade" was invisible to the user.
    """
    full_prompt = f"{_VISION_SYSTEM}\n\nUser asks: {question}"

    # 1. Local qwen3-vl:4b — V19 primary (GUI-trained, OSWorld-benchmarked).
    #    ~4s on RTX 4060 Laptop, fits in 8 GB VRAM alongside Hermes 3 (with
    #    _local_model_lock serializing). No quota cost.
    out = _ollama_vision(full_prompt, b64_img, timeout=30)
    if out and len(out.strip()) > 10:
        logger.info("vision: answered via local %s (V19 primary)", VISION_MODEL_OLLAMA)
        return out
    if out:
        logger.info("vision: local %s returned short reply (%d chars) — trying brief prompt",
                    VISION_MODEL_OLLAMA, len(out.strip()))

    # 1b. One retry with a simpler prompt before falling to cloud — qwen3-vl
    #     occasionally returns empty on complex prompts; brief is usually
    #     enough to unstick it.
    brief = "What's the most important thing visible on this screen? One sentence."
    out2 = _ollama_vision(brief, b64_img, timeout=20)
    if out2 and len(out2.strip()) > 10:
        logger.info("vision: answered via local %s on brief-prompt retry", VISION_MODEL_OLLAMA)
        return out2

    # 2. Gemini Flash — cloud fallback only when local truly failed.
    logger.info("vision: local %s unavailable/empty, falling back to Gemini Flash",
                VISION_MODEL_OLLAMA)
    out = _gemini_vision(full_prompt, b64_img)
    if out:
        logger.info("vision: answered via Gemini Flash (cloud fallback)")
        return out

    return ""


# ── Public API ──────────────────────────────────────────────────────────────
def look_at_screen(question: str = "What's on the screen right now?") -> str:
    """The main entry point: capture + analyze + answer."""
    b64 = _capture_b64()
    if not b64:
        return "I couldn't capture the screen right now."
    ans = _ask_vision(question, b64)
    if not ans:
        # V19: more accurate error — qwen3-vl:4b is primary now.
        return (f"My local vision model ({VISION_MODEL_OLLAMA}) didn't respond and "
                f"the Gemini fallback also failed. Check that Ollama is running and "
                f"`ollama list` shows {VISION_MODEL_OLLAMA}.")
    # V20 Step 5: cache the description for ~30s so the Cerebras planner can
    # answer "click the gameplay one" without taking a new screenshot.
    try:
        import runtime_context
        runtime_context.set_screen_context(ans)
    except Exception:
        pass
    return ans


def describe_screen() -> str:
    """Free-form: 'tell me what you see'."""
    return look_at_screen(
        "Describe what's on this screen in 2-3 short sentences — the main app, "
        "what the user appears to be doing, and anything notable. Be specific "
        "(actual text, app names, button labels) — do NOT guess."
    )


def read_text_on_screen() -> str:
    """OCR-style: read all visible text."""
    return look_at_screen(
        "Read the visible text on this screen out loud (the main / important "
        "text the user is likely looking at). Skip menus and UI chrome. "
        "Be accurate — copy text exactly, don't paraphrase."
    )


def find_on_screen(target: str) -> str:
    """Locate a UI element. Returns a description; the agent decides what to do."""
    return look_at_screen(
        f"Look for '{target}' on this screen. If you see it, describe exactly "
        f"where it is (which corner, what's around it, what color). If it's a "
        f"clickable element, say so. If you don't see it, say so honestly."
    )


def vision_provider_status() -> dict:
    return {
        "ollama_model":  VISION_MODEL_OLLAMA,
        "ollama_url":    OLLAMA_URL,
        "max_dimension": MAX_DIMENSION,
        "timeout_s":     VISION_TIMEOUT,
    }


# ── Pre-warm the vision model so first call isn't a cold-start ─────────────
def prewarm_vision() -> None:
    """Background: load qwen2.5vl into VRAM at boot."""
    def _warm():
        try:
            requests.post(
                OLLAMA_URL,
                json={
                    "model": VISION_MODEL_OLLAMA,
                    "messages": [{"role": "user", "content": "ready?"}],
                    "stream": False,
                    "keep_alive": VISION_KEEP_ALIVE,
                },
                timeout=120,
            )
            logger.info("Vision model '%s' pre-warmed (keep_alive=%s).",
                        VISION_MODEL_OLLAMA, VISION_KEEP_ALIVE)
        except Exception as e:
            logger.info("Vision pre-warm skipped: %s", str(e)[:120])
    threading.Thread(target=_warm, daemon=True, name="vision-prewarm").start()
