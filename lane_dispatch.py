"""
lane_dispatch.py — V19

Single entry point for the 6-lane brain router. The brain layer calls
dispatch(text, history, think_mode_on) and gets back (reply_text, info).
The lane_classifier picks the lane; this module owns the provider calls
and fallback chain.

Fallback order when the chosen lane returns "":
  github_premium  → cerebras_120b → nim_nemotron → ollama (hermes)
  groq_8b         → cerebras_120b → nim_nemotron → ollama
  cerebras_120b   → nim_nemotron → groq_8b → ollama
  nim_nemotron    → cerebras_120b → groq_8b → ollama
  hermes_tools    → handled by agent loop (this module not used)
  vision          → handled by vision_tools (this module not used)
"""

from __future__ import annotations
import logging

logger = logging.getLogger(__name__)


def _call_cerebras(messages, max_tokens=300):
    # Use the existing agent._cerebras_chat for compatibility, but we have
    # messages already shaped — bypass to a direct call here.
    try:
        import requests, config
        # Quota guard via budget
        try:
            from budget import would_overflow_cerebras
            if would_overflow_cerebras(messages):
                logger.info("dispatch: cerebras 8K guard tripped — skipping")
                return ""
        except Exception:
            pass
        key = getattr(config, "CEREBRAS_API_KEY", "")
        if not key: return ""
        model = getattr(config, "CEREBRAS_MODEL", "gpt-oss-120b")
        r = requests.post(
            getattr(config, "CEREBRAS_URL", "https://api.cerebras.ai/v1/chat/completions"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages,
                  "max_completion_tokens": max_tokens, "temperature": 0.6, "stream": False},
            timeout=getattr(config, "CEREBRAS_TIMEOUT", 10),
        )
        if r.status_code != 200:
            logger.info("dispatch.cerebras HTTP %d", r.status_code)
            return ""
        msg = r.json().get("choices", [{}])[0].get("message", {}) or {}
        # gpt-oss-120b is a reasoning model: if content empty (cut by token
        # budget mid-think), fall back to the reasoning field.
        return (msg.get("content") or msg.get("reasoning") or "").strip()
    except Exception as e:
        logger.info("dispatch.cerebras error: %s", e)
        return ""


def _call_groq(messages, max_tokens=300):
    try:
        import groq_lane
        return groq_lane.chat(messages, max_tokens=max_tokens)
    except Exception as e:
        logger.info("dispatch.groq error: %s", e); return ""


def _call_github(messages, max_tokens=600):
    try:
        import github_lane
        return github_lane.chat(messages, max_tokens=max_tokens)
    except Exception as e:
        logger.info("dispatch.github error: %s", e); return ""


def _call_nim(messages, max_tokens=500):
    try:
        import nim_lane
        return nim_lane.chat(messages, max_tokens=max_tokens)
    except Exception as e:
        logger.info("dispatch.nim error: %s", e); return ""


def _build_messages(text: str, history: list, system: str | None) -> list[dict]:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    for h in (history or [])[-8:]:
        msgs.append({"role": h.get("role", "user"), "content": h.get("content", "")})
    msgs.append({"role": "user", "content": text})
    return msgs


def dispatch(text: str, history: list, lane: str,
             system: str | None = None) -> tuple[str, dict]:
    """
    Run the chosen lane with documented fallbacks. Returns (reply, info_dict).
    info_dict["lane_used"] = the lane that actually answered (may differ from
    requested if a fallback succeeded).
    """
    messages = _build_messages(text, history, system)
    info: dict = {"lane_requested": lane, "lane_used": None}

    # Hermes tools / vision are NOT routed through this dispatcher
    if lane in ("hermes_tools", "vision"):
        info["lane_used"] = lane
        info["note"]      = "handled outside dispatch"
        return "", info

    chain_map = {
        "github_premium": [("github_premium", _call_github),
                            ("cerebras_120b",  _call_cerebras),
                            ("nim_nemotron",   _call_nim)],
        "groq_8b":         [("groq_8b",        _call_groq),
                            ("cerebras_120b",  _call_cerebras),
                            ("nim_nemotron",   _call_nim)],
        "cerebras_120b":   [("cerebras_120b",  _call_cerebras),
                            ("nim_nemotron",   _call_nim),
                            ("groq_8b",        _call_groq)],
        "nim_nemotron":    [("nim_nemotron",   _call_nim),
                            ("cerebras_120b",  _call_cerebras),
                            ("groq_8b",        _call_groq)],
    }
    chain = chain_map.get(lane) or chain_map["cerebras_120b"]

    for lane_name, fn in chain:
        reply = fn(messages)
        if reply:
            info["lane_used"] = lane_name
            info["fallback"]  = (lane_name != lane)
            # Remember lane + utterance for follow-up inheritance next turn.
            # V19 BUG-3b: passing `text` enables topic-noun overlap check.
            try:
                import lane_classifier
                lane_classifier.remember_lane(lane_name, utterance=text)
            except Exception:
                pass
            return reply, info

    info["lane_used"] = "none"
    info["error"]     = "all_lanes_failed"
    return "", info
