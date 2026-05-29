"""
nim_lane.py — V19 Step 6

NVIDIA NIM as the overflow lane. NOT a "deep reason" lane — purely the
safety net when:
  - Cerebras 8K context guard trips (budget.would_overflow_cerebras True)
  - Groq daily cap hit (req or tokens)
  - GitHub Models daily cap hit (Think mode active but over limit)

Default model: nemotron-nano-9b-v2 (~30-90 tok/s, no token cap on free
tier, 1000 starter credits). Burn-rate logged via budget.nim_record_call
so the user can see when credits are running low. We do NOT pre-select a
second NIM model — we wait for actual credit-burn data before deciding.
"""

from __future__ import annotations
import logging, os
import requests

logger = logging.getLogger(__name__)

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "").strip()
NIM_CHAT_URL   = "https://integrate.api.nvidia.com/v1/chat/completions"
NIM_MODEL      = os.getenv("NIM_MODEL", "nvidia/nemotron-nano-9b-v2")


def available() -> bool:
    return bool(NVIDIA_API_KEY)


def chat(messages: list[dict], max_tokens: int = 500, temperature: float = 0.6,
         timeout: float = 30.0) -> str:
    """OpenAI-compatible chat completion via NVIDIA NIM. Returns '' on
    failure (caller falls through to Gemini or Ollama).

    V19: Nemotron Nano 9B v2 is a reasoning model that emits internal
    thinking traces in `message.reasoning`. For Maki's voice-assistant use
    case we want fast, direct answers — so we inject the Nemotron-documented
    `/no_think` system token to disable reasoning mode. If a system message
    is already provided, we prepend `/no_think\\n` to it instead of adding
    a second system message."""
    if not NVIDIA_API_KEY:
        return ""

    # Inject /no_think for Nemotron reasoning models
    if "nemotron" in NIM_MODEL.lower():
        mlist = list(messages)
        if mlist and mlist[0].get("role") == "system":
            mlist[0] = {"role":"system",
                        "content": "/no_think\n" + (mlist[0].get("content","") or "")}
        else:
            mlist.insert(0, {"role":"system","content":"/no_think"})
        messages = mlist

    # Breadcrumb
    try:
        from breadcrumb import trail
    except Exception:
        from contextlib import nullcontext as trail
        def _t(*a, **kw): return trail()
        trail = _t   # type: ignore

    with trail("NIM", "chat", model=NIM_MODEL):
        try:
            r = requests.post(
                NIM_CHAT_URL,
                headers={"Authorization": f"Bearer {NVIDIA_API_KEY}",
                         "Content-Type": "application/json",
                         "Accept": "application/json"},
                json={"model": NIM_MODEL, "messages": messages,
                      "max_tokens": max_tokens, "temperature": temperature,
                      "stream": False},
                timeout=timeout,
            )
            # Record one credit consumed regardless of outcome (NIM debits
            # on attempt). Burn-rate alarm fires inside budget when remaining
            # drops below 100.
            try:
                from budget import nim_record_call
                nim_record_call(credits_cost=1)
            except Exception:
                pass

            if r.status_code != 200:
                logger.info("NIM HTTP %d: %s", r.status_code, r.text[:160])
                return ""
            data = r.json()
            msg = data.get("choices", [{}])[0].get("message", {}) or {}
            # Some reasoning models leave content=null and put output in
            # reasoning when /no_think was bypassed. Fall back to reasoning.
            text = (msg.get("content") or msg.get("reasoning") or "").strip()
            return text
        except Exception as e:
            logger.info("NIM error: %s", e)
            return ""


def credits_remaining() -> int:
    try:
        from budget import nim_credits_remaining
        return nim_credits_remaining()
    except Exception:
        return -1
