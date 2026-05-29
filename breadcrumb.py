"""
breadcrumb.py — V19 Step 1.5

Lightweight observability for V19 new code paths only (vision, Groq chat,
Groq Whisper STT, NIM, GitHub Models). NOT a full crash logger.

Purpose: if a V19 step regresses, we can attribute it. We log start/end of
each instrumented call with subsystem, action, duration, and pid. Append-only
JSONL so partial writes never corrupt history.

Usage:
    from breadcrumb import trail
    with trail("VISION", "qwen3_vl_describe"):
        result = vision_call(...)

    # Or as a no-context one-shot:
    breadcrumb.note("GROQ_CHAT", "rate_limited", extra={"retry_after": 12})
"""

from __future__ import annotations
import json, os, time, threading
from contextlib import contextmanager
from pathlib import Path

_LOG_DIR = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_LOG     = _LOG_DIR / "v19_actions.jsonl"
_MAX_BYTES = 1_000_000   # rotate at ~1MB

_lock = threading.Lock()
_pid  = os.getpid()


def _rotate_if_needed() -> None:
    try:
        if _LOG.exists() and _LOG.stat().st_size > _MAX_BYTES:
            _LOG.rename(_LOG.with_suffix(".jsonl.prev"))
    except Exception:
        pass


def _write(entry: dict) -> None:
    try:
        with _lock:
            _rotate_if_needed()
            with open(_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass   # observability must never crash the caller


def note(subsystem: str, action: str, **extra) -> None:
    """One-shot breadcrumb (no duration). Use for events, not calls."""
    _write({
        "ts":        time.time(),
        "pid":       _pid,
        "subsystem": subsystem,
        "action":    action,
        "kind":      "note",
        **extra,
    })


@contextmanager
def trail(subsystem: str, action: str, **extra):
    """
    Context manager. Logs start, then end with duration_ms and ok flag.
    If an exception escapes, logs ok=False with exc type/message.
    """
    started = time.time()
    _write({
        "ts":        started,
        "pid":       _pid,
        "subsystem": subsystem,
        "action":    action,
        "kind":      "start",
        **extra,
    })
    ok = True
    err = None
    try:
        yield
    except Exception as e:
        ok = False
        err = f"{type(e).__name__}: {str(e)[:200]}"
        raise
    finally:
        _write({
            "ts":           time.time(),
            "pid":          _pid,
            "subsystem":    subsystem,
            "action":       action,
            "kind":         "end",
            "duration_ms":  int((time.time() - started) * 1000),
            "ok":           ok,
            "error":        err,
        })
