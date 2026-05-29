"""
onnx_embedder.py — V15 fast local embeddings via ONNX Runtime.

Replaces the 2-second-per-call Ollama nomic-embed-text path with
BGE-small-en-v1.5 ONNX (~33M params, ~10-30ms per embed on CPU).

Uses only `onnxruntime` + `tokenizers` (no torch, no transformers).
"""

from __future__ import annotations
import logging, os
from typing import Optional
import numpy as np

logger = logging.getLogger(__name__)

_MODEL_DIR  = os.path.join(os.path.dirname(__file__), "models", "bge-small-en")
_MODEL_PATH = os.path.join(_MODEL_DIR, "onnx", "model.onnx")
_TOKZ_PATH  = os.path.join(_MODEL_DIR, "tokenizer.json")
_MAX_LEN    = 64   # voice commands are short; keep this tight for speed

_session = None
_tokz    = None


def _load():
    global _session, _tokz
    if _session is not None: return
    try:
        import onnxruntime as ort
        from tokenizers import Tokenizer
    except ImportError as e:
        raise RuntimeError(f"onnxruntime / tokenizers missing: {e}")
    if not os.path.exists(_MODEL_PATH):
        raise RuntimeError(f"BGE ONNX model not found at {_MODEL_PATH}. "
                            f"Run the download step first.")
    sess_opts = ort.SessionOptions()
    sess_opts.intra_op_num_threads = 4
    sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    _session = ort.InferenceSession(_MODEL_PATH,
                                     sess_options=sess_opts,
                                     providers=["CPUExecutionProvider"])
    _tokz = Tokenizer.from_file(_TOKZ_PATH)
    _tokz.enable_truncation(max_length=_MAX_LEN)
    _tokz.enable_padding(length=_MAX_LEN)
    logger.info("onnx_embedder: loaded BGE-small-en-v1.5 (ONNX, CPU)")


def embed(text: str) -> Optional[np.ndarray]:
    """Returns a unit-normalized 384-dim embedding for `text`, or None."""
    if not text or not text.strip():
        return None
    if _session is None: _load()
    enc = _tokz.encode(text.strip().lower())
    ids   = np.asarray([enc.ids],            dtype=np.int64)
    mask  = np.asarray([enc.attention_mask], dtype=np.int64)
    type_ids = np.zeros_like(ids)
    outputs = _session.run(None, {
        "input_ids":      ids,
        "attention_mask": mask,
        "token_type_ids": type_ids,
    })
    # BGE uses [CLS] (first token) pooling, then L2-normalize
    cls = outputs[0][0, 0, :]   # [batch=1, seq=0 (CLS), hidden]
    n = np.linalg.norm(cls)
    if n == 0: return None
    return (cls / n).astype(np.float32)


def is_available() -> bool:
    try:
        _load()
        return _session is not None
    except Exception:
        return False
