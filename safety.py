"""
safety.py - Block genuinely dangerous actions.
Uses word-boundary matching to prevent false positives like
"information" triggering the "format" keyword.
"""

import re, logging
import config

logger = logging.getLogger(__name__)

# Pre-compile patterns for speed
_PATTERNS = []
for _kw in config.RISKY_KEYWORDS:
    if " " in _kw:
        # Multi-word phrase — substring match is fine (phrases don't appear inside other words)
        _PATTERNS.append(re.compile(re.escape(_kw), re.I))
    else:
        # Single word — require word boundary so "format" doesn't match "information"
        _PATTERNS.append(re.compile(r"\b" + re.escape(_kw) + r"\b", re.I))


def is_risky(text: str) -> bool:
    return any(p.search(text) for p in _PATTERNS)


def risky_response(text: str) -> str:
    logger.warning("Risky action blocked: %s", text)
    return (
        "I can't do that — it involves something I'm not allowed to touch, "
        "like sending messages, buying things, or making permanent changes. "
        "Let me know if you need something else."
    )
