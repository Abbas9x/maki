"""
local_lock.py — V19 Step 2.5

Serialization for local Ollama models on an 8 GB VRAM card.

V19 loads two large local models:
  - hermes3:8b      (~5.2 GB)  — tool calls / chat fallback
  - qwen3-vl:4b     (~3.3 GB)  — vision

Combined that's 8.5 GB > 8 GB card. OLLAMA_KEEP_ALIVE alone is NOT a
serialization primitive — it's just an idle-timeout. If a tool-call routes
to Hermes while a vision call is still in flight, Ollama tries to load both
and one OOMs (silently on Windows, sometimes as a SIGSEGV).

This module provides ONE process-wide lock. Both call sites acquire it
before contacting Ollama. Only one local model is in flight at a time.

The lock has a generous timeout (90s) — long enough for a slow vision call
to finish, short enough that a stuck call doesn't deadlock the agent.
"""

from __future__ import annotations
import logging, threading
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_ACQUIRE_TIMEOUT = 90.0   # seconds


@contextmanager
def local_model_slot(who: str):
    """
    Acquire the single local-model slot. `who` is a label for logging
    ('vision', 'hermes_agent', 'hermes_chitchat', etc.).

    If acquisition times out (90s), we yield anyway and log a warning.
    This is a soft serialization, not a hard guarantee — we prefer
    "two concurrent calls and one fails noisily" over "deadlocked agent".
    """
    got = _LOCK.acquire(timeout=_ACQUIRE_TIMEOUT)
    if not got:
        logger.warning("local_lock: %s waited >%.0fs — proceeding without slot", who, _ACQUIRE_TIMEOUT)
    try:
        yield got
    finally:
        if got:
            try:
                _LOCK.release()
            except RuntimeError:
                pass
