"""
github_lane.py — V19 Step 5

GitHub Models free-tier as the Think-mode brain.

When the user presses the 🧠 Think toggle, the brain routes hard/deep
reasoning turns here instead of Cerebras. We default to Claude Sonnet 4.5
because it instruction-follows conversational voice-assistant prompts better
than GPT-4o. Override with the THINK_MODEL env var:
  THINK_MODEL=gpt-4o            (OpenAI's flagship)
  THINK_MODEL=gpt-4.1
  THINK_MODEL=o3                 (deep reasoning)
  THINK_MODEL=claude-sonnet-4.5  (default)

Free tier caps: 50-150 req/day depending on model (varies by tier and
GitHub account status). On exhaustion we return "" so the brain falls
through to Cerebras with a one-time UI hint.

API surface is OpenAI-compatible via models.github.ai.
"""

from __future__ import annotations
import datetime, logging, os, threading
import requests

logger = logging.getLogger(__name__)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_MODELS_URL = "https://models.github.ai/inference/chat/completions"

# Default: Claude Sonnet 4.5 (slot name as exposed by GitHub Models)
THINK_MODEL = os.getenv("THINK_MODEL", "anthropic/claude-sonnet-4.5")

# Free-tier daily req cap (conservative; varies by model)
_DAILY_CAP_REQ = int(os.getenv("GITHUB_MODELS_DAILY_CAP", "50"))

_lock = threading.Lock()
_state = {"date": None, "req_today": 0, "warned_user": False}


def _reset_if_new_day() -> None:
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    if _state["date"] != today:
        _state["date"]        = today
        _state["req_today"]   = 0
        _state["warned_user"] = False


def available() -> bool:
    return bool(GITHUB_TOKEN)


def quota_ok() -> tuple[bool, str]:
    with _lock:
        _reset_if_new_day()
        if _state["req_today"] >= _DAILY_CAP_REQ:
            return False, f"github_models daily cap reached ({_DAILY_CAP_REQ})"
        return True, "ok"


def mark_warned() -> None:
    """Brain calls this after surfacing the 'think quota reached' UI hint
    so we only show it once per day."""
    with _lock:
        _state["warned_user"] = True


def should_warn() -> bool:
    with _lock:
        return not _state["warned_user"]


def chat(messages: list[dict], max_tokens: int = 600, temperature: float = 0.6,
         timeout: float = 12.0) -> str:
    """OpenAI-compatible chat completion via GitHub Models. Returns '' on any
    failure so the caller can fall through to Cerebras."""
    if not GITHUB_TOKEN:
        return ""
    ok, reason = quota_ok()
    if not ok:
        logger.info("github_lane: %s — falling through", reason)
        return ""

    # Breadcrumb
    try:
        from breadcrumb import trail
    except Exception:
        from contextlib import nullcontext as trail
        def _t(*a, **kw): return trail()
        trail = _t   # type: ignore

    with trail("GITHUB_MODELS", "chat", model=THINK_MODEL):
        try:
            r = requests.post(
                GITHUB_MODELS_URL,
                headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                         "Content-Type": "application/json",
                         "Accept": "application/json"},
                json={"model": THINK_MODEL, "messages": messages,
                      "max_tokens": max_tokens, "temperature": temperature},
                timeout=timeout,
            )
            with _lock:
                _reset_if_new_day()
                _state["req_today"] += 1
            if r.status_code == 429:
                logger.info("github_lane: 429 rate-limited — falling through")
                return ""
            if r.status_code != 200:
                logger.info("GitHub Models HTTP %d: %s", r.status_code, r.text[:160])
                return ""
            data = r.json()
            return (data.get("choices", [{}])[0]
                        .get("message", {}).get("content", "") or "").strip()
        except Exception as e:
            logger.info("github_lane error: %s", e)
            return ""


def remaining_today() -> int:
    with _lock:
        _reset_if_new_day()
        return max(0, _DAILY_CAP_REQ - _state["req_today"])
