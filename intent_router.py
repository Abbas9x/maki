"""
intent_router.py — V15 semantic intent routing.

Replaces 50+ hardcoded regex patterns with embedding-based intent matching.
User says ANYTHING → embed → cosine-similarity to canonical example phrases
for each intent → route to the highest-matching intent (above threshold).

Catches all natural phrasings of the same intent with ZERO regex:
  "go to chrome" / "switch to chrome" / "take me to chrome" / "bring up chrome"
  → all match the "focus_app" intent.

Architecture:
  - At startup: embed all canonical example utterances once (cached on disk).
  - At runtime: embed the user's message (~30-50ms via local Ollama nomic-embed-text),
    cosine-compare to all examples, pick the highest-matching intent.
  - If similarity < threshold, return None → falls through to LLM agent.

Speed: ~50ms per route decision (embedding) + <1ms compare.
"""

from __future__ import annotations
import json, logging, os, pickle, threading, time
from dataclasses import dataclass, field
from typing import Optional, Callable

import numpy as np
import requests

logger = logging.getLogger(__name__)

OLLAMA_EMBED_URL  = os.getenv("OLLAMA_EMBED_URL", "http://localhost:11434/api/embeddings")
EMBED_MODEL       = os.getenv("EMBED_MODEL", "nomic-embed-text")
CACHE_PATH        = os.path.join(os.path.dirname(__file__), "logs", "intent_embeddings.pkl")
DEFAULT_THRESHOLD = 0.65   # below this → fall through


@dataclass
class Intent:
    """One intent with example utterances + the action to run."""
    name: str
    examples: list[str]
    handler: Callable[[str], Optional[str]]   # called with the original text
    threshold: float = DEFAULT_THRESHOLD
    negatives: list[str] = field(default_factory=list)  # phrases that LOOK similar but DON'T match


class IntentRouter:
    def __init__(self):
        self._intents: list[Intent] = []
        self._example_vecs: dict[str, np.ndarray] = {}   # phrase → vec
        self._intent_for_phrase: dict[str, str] = {}     # phrase → intent name
        self._negative_vecs: dict[str, list[np.ndarray]] = {}   # name → vecs
        self._lock = threading.Lock()
        self._ready = False

    def register(self, intent: Intent):
        self._intents.append(intent)

    # ── embedding ───────────────────────────────────────────────────────
    # V15.1: prefer local ONNX BGE-small (10-30ms) over Ollama nomic (2s on Windows).
    _onnx_ok = None
    @classmethod
    def _embed(cls, text: str) -> Optional[np.ndarray]:
        # First-call probe of ONNX availability (cached)
        if cls._onnx_ok is None:
            try:
                import onnx_embedder
                cls._onnx_ok = onnx_embedder.is_available()
                if cls._onnx_ok:
                    logger.info("intent_router: using ONNX BGE-small embedder (fast)")
                else:
                    logger.info("intent_router: ONNX unavailable, using Ollama nomic (slow)")
            except Exception as e:
                logger.info("intent_router: ONNX probe failed: %s — using Ollama", e)
                cls._onnx_ok = False

        if cls._onnx_ok:
            try:
                import onnx_embedder
                v = onnx_embedder.embed(text)
                if v is not None: return v
            except Exception as e:
                logger.info("intent_router: ONNX embed failed for %r — %s", text[:40], e)
                # one-time fall back to Ollama path
                cls._onnx_ok = False

        # Ollama fallback
        try:
            r = requests.post(OLLAMA_EMBED_URL,
                              json={"model": EMBED_MODEL, "prompt": text},
                              timeout=5)
            r.raise_for_status()
            v = r.json().get("embedding") or []
            if not v: return None
            arr = np.asarray(v, dtype=np.float32)
            n = np.linalg.norm(arr)
            return arr / n if n > 0 else None
        except Exception as e:
            logger.info("intent_router: Ollama embed failed for %r — %s", text[:40], e)
            return None

    # ── build / load cache ──────────────────────────────────────────────
    def prepare(self):
        """Embed all examples (uses on-disk cache when content unchanged)."""
        with self._lock:
            if self._ready: return
            # Cache key = sha of all (intent_name, examples, negatives)
            import hashlib
            key_parts = []
            for it in self._intents:
                key_parts.append(it.name)
                key_parts.extend(it.examples)
                key_parts.extend(it.negatives or [])
            content_hash = hashlib.sha256("|".join(key_parts).encode()).hexdigest()

            cached = self._load_cache(content_hash)
            if cached is not None:
                self._example_vecs, self._intent_for_phrase, self._negative_vecs = cached
                logger.info("intent_router: loaded %d example embeddings from cache",
                            len(self._example_vecs))
                self._ready = True
                return

            logger.info("intent_router: building embeddings for %d intents…",
                        len(self._intents))
            t0 = time.time()
            for it in self._intents:
                for ex in it.examples:
                    v = self._embed(ex)
                    if v is not None:
                        self._example_vecs[ex] = v
                        self._intent_for_phrase[ex] = it.name
                if it.negatives:
                    self._negative_vecs[it.name] = []
                    for neg in it.negatives:
                        v = self._embed(neg)
                        if v is not None:
                            self._negative_vecs[it.name].append(v)
            logger.info("intent_router: embedded %d phrases in %.1fs",
                        len(self._example_vecs), time.time() - t0)
            self._save_cache(content_hash)
            self._ready = True

    def _load_cache(self, content_hash: str):
        if not os.path.exists(CACHE_PATH): return None
        try:
            with open(CACHE_PATH, "rb") as f:
                d = pickle.load(f)
            if d.get("hash") == content_hash:
                return (d["example_vecs"], d["intent_for_phrase"], d["negative_vecs"])
        except Exception as e:
            logger.info("intent_router: cache read failed: %s", e)
        return None

    def _save_cache(self, content_hash: str):
        try:
            os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
            with open(CACHE_PATH, "wb") as f:
                pickle.dump({
                    "hash": content_hash,
                    "example_vecs": self._example_vecs,
                    "intent_for_phrase": self._intent_for_phrase,
                    "negative_vecs": self._negative_vecs,
                }, f)
        except Exception as e:
            logger.info("intent_router: cache write failed: %s", e)

    # ── V19 Step 4: classify-only (no handler invocation) ──────────────
    def classify(self, text: str) -> Optional[tuple[str, float]]:
        """Return (intent_name, best_score) without running the handler.
        Used by lane_classifier to pick a lane before deciding whether to
        execute the intent or route the utterance to a brain lane."""
        if not self._ready: self.prepare()
        if not self._example_vecs: return None
        vec = self._embed(text)
        if vec is None: return None
        best_phrase, best_score = None, -1.0
        for phrase, ev in self._example_vecs.items():
            score = float(np.dot(vec, ev))
            if score > best_score:
                best_score, best_phrase = score, phrase
        if best_phrase is None: return None
        intent_name = self._intent_for_phrase[best_phrase]
        return intent_name, best_score

    # ── route ───────────────────────────────────────────────────────────
    def route(self, text: str) -> Optional[str]:
        """Returns the spoken reply if an intent matched above threshold, else None."""
        if not self._ready: self.prepare()
        if not self._example_vecs: return None
        vec = self._embed(text)
        if vec is None: return None

        # Find best matching example (highest cosine)
        best_phrase = None
        best_score = -1.0
        for phrase, ev in self._example_vecs.items():
            score = float(np.dot(vec, ev))
            if score > best_score:
                best_score = score
                best_phrase = phrase

        if best_phrase is None: return None
        intent_name = self._intent_for_phrase[best_phrase]
        intent = next((i for i in self._intents if i.name == intent_name), None)
        if intent is None: return None

        # Negative check — if the message is closer to a negative example for this
        # intent than any positive example, reject. (Mitigates "open vs close" trap.)
        neg_vecs = self._negative_vecs.get(intent_name, [])
        if neg_vecs:
            neg_score = max(float(np.dot(vec, nv)) for nv in neg_vecs)
            if neg_score > best_score - 0.02:   # too close to a "don't match" phrase
                logger.info("intent_router: rejected '%s' (neg_score %.2f >= pos %.2f)",
                            text[:60], neg_score, best_score)
                return None

        # Threshold check
        if best_score < intent.threshold:
            return None

        logger.info("intent_router: '%s' → %s (score %.2f, ex='%s')",
                    text[:60], intent_name, best_score, best_phrase[:40])
        try:
            return intent.handler(text)
        except Exception as e:
            logger.warning("intent_router: handler '%s' failed: %s", intent_name, e)
            return None


# ── Background prep so first user query isn't a cold start ──────────────────
def prewarm(router: IntentRouter):
    def _go():
        try: router.prepare()
        except Exception as e: logger.info("intent prewarm: %s", e)
    threading.Thread(target=_go, daemon=True, name="intent-prewarm").start()
