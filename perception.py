"""
perception.py — V16: the "think before acting" layer.

Problem (from real logs):
  - Whisper hears "scroll the chrome" as "school of chrome".
  - Maki takes the mishearing literally and asks about a school website.
  - "type wikipedia" gets heard as "time for wikipedia", "i am wikipedia",
    "hype wikipedia" — each interpreted in isolation with no context.
  - "wikipedia" alone, RIGHT AFTER searching for wikipedia → Maki treats it
    as a question instead of inferring "click the wikipedia link".

Solution: one fast LLM call (Cerebras gpt-oss-120b, ~500ms) that:
  1. Reads the raw transcript + current context (focused app, last action,
     recent transcripts).
  2. Returns a CORRECTED + DISAMBIGUATED interpretation of what the user
     most likely meant.
  3. Or flags low confidence and tells Maki to ask for clarification.

The output of this layer is what gets fed to the intent router. So even when
Whisper screws up the transcript, Maki "hears" the right thing.

Public API:
  perceive(transcript) -> Perception
    .corrected_text : str   — best guess at what user actually said
    .is_garbage     : bool  — transcript is nonsense
    .confidence     : float — 0.0-1.0
    .clarify        : str   — if confidence is low, the question to ask
    .reasoning      : str   — for logging/debugging
"""
from __future__ import annotations
import json, logging, re, threading, time
from dataclasses import dataclass
from typing import Optional

import requests
import config

logger = logging.getLogger(__name__)


@dataclass
class Perception:
    corrected_text: str
    is_garbage:     bool
    confidence:     float       # 0.0-1.0
    clarify:        str         # clarifying question if confidence is low
    reasoning:      str         # one-line explanation for logging
    raw_input:      str         # the original transcript


# ── Context bundle ──────────────────────────────────────────────────────────
class Context:
    """Tiny store of recent state the perception layer can use."""
    def __init__(self):
        self._lock = threading.Lock()
        self.last_user_text:    str = ""
        self.last_assistant:    str = ""
        self.last_action:       str = ""    # e.g. "search_youtube('mrbeast')"
        self.foreground_app:    str = ""
        self.window_title:      str = ""

    def snapshot(self) -> dict:
        # Refresh foreground app lazily — cheap UIA call
        try:
            import ui_tree
            sum_ = ui_tree.foreground_app_summary()
            # Parse "Foreground: <title> — N interactive elements"
            m = re.match(r"Foreground:\s*(.+?)\s*—", sum_)
            if m:
                self.window_title = m.group(1).strip()
        except Exception:
            pass
        with self._lock:
            return {
                "last_user_text":  self.last_user_text,
                "last_assistant":  self.last_assistant,
                "last_action":     self.last_action,
                "window_title":    self.window_title,
            }

    def update(self, *, user: str = None, assistant: str = None, action: str = None):
        with self._lock:
            if user      is not None: self.last_user_text = user
            if assistant is not None: self.last_assistant = assistant
            if action    is not None: self.last_action = action


ctx = Context()


# ── Cerebras perception call ────────────────────────────────────────────────
_PERCEPTION_SYSTEM = """You are a voice-assistant transcript interpreter for Maki, a Windows voice assistant.
The user just spoke into a microphone. The speech-to-text may have errors.
Your job: figure out what the user MOST LIKELY meant, given the context, and
REWRITE the command into something Maki can execute directly.

You must reply with STRICT JSON only (no markdown, no commentary):
{
  "corrected": "the cleanest version of what the user probably said",
  "is_garbage": false,
  "confidence": 0.0,
  "clarify": "",
  "reasoning": "one short sentence"
}

RULES:
1. **Fix obvious mishearings** using context. Examples:
   - "school of chrome" + last action was scroll → "scroll the chrome page", confidence 0.85
   - "brain chrome" / "ring chrome" → "bring chrome to front", confidence 0.9
   - "and use this on google chrome" → "open google chrome", confidence 0.7
   - "press on muhammad" → "click on muhammad", confidence 0.9
   - "i'll put in whatsapp" → "open whatsapp", confidence 0.75
2. **EXPAND PRONOUNS** to actual entity names from context. CRITICAL.
   Examples:
   - Last user: "time in pakistan and london and egypt"
     New: "weather for them" → CORRECTED: "weather in pakistan, london and egypt"
   - Last user: "weather in tokyo"
     New: "convert to celsius" → CORRECTED: "convert the tokyo temperature to celsius"
   - Last assistant mentions "Pakistan, London, Egypt"
     New: "weather in all those countries" → CORRECTED: "weather in pakistan, london, egypt"
3. **Use last_action/window_title to disambiguate**:
   - Just searched "wikipedia" → "wikipedia" alone → "click the wikipedia link"
   - Just opened a new tab → "type wikipedia" → "type wikipedia"
   - Window is Chrome → "scroll" → "scroll down"
4. **For date/time arithmetic** (e.g. "what day is the 29th"), corrected
   should be the literal question — Maki's agent handles those.
5. If the transcript is nonsense, set is_garbage: true, confidence: 0.0,
   and a friendly clarify like "Sorry, I missed that — could you say it again?".
6. DO NOT invent actions the user didn't ask for. Stay close to intent.
7. Confidence guide: 0.9+ = sure, 0.7-0.9 = probable, 0.5-0.7 = ambiguous
   (set a clarify question), <0.5 = unclear (is_garbage often true).
"""


def _cerebras_call(prompt: str, system: str = _PERCEPTION_SYSTEM,
                    max_tokens: int = 250, timeout: float = 8.0) -> str:
    key = getattr(config, "CEREBRAS_API_KEY", "")
    if not key: return ""
    model = getattr(config, "CEREBRAS_MODEL", "gpt-oss-120b")
    messages = [{"role": "system", "content": system},
                {"role": "user",   "content": prompt}]
    # V19 Step 1: 8K context guard. Cerebras free-tier silently truncates
    # at 8192 tokens. If projected > 7500 we skip the call — perception's
    # fallback is to return "" and the pipeline uses the raw transcript.
    # Step 6 will convert this skip into a reroute to NIM Nemotron Nano 9B.
    try:
        from budget import would_overflow_cerebras
        if would_overflow_cerebras(messages):
            logger.info("perception: 8K guard tripped — skipping Cerebras, using raw transcript")
            return ""
    except Exception:
        pass
    try:
        r = requests.post(
            getattr(config, "CEREBRAS_URL",
                    "https://api.cerebras.ai/v1/chat/completions"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model,
                  "messages": messages,
                  "max_completion_tokens": max_tokens,
                  "temperature": 0.1, "stream": False},
            timeout=timeout,
        )
        if r.status_code != 200:
            logger.info("perception: Cerebras HTTP %d", r.status_code)
            return ""
        return (r.json().get("choices",[{}])[0]
                       .get("message",{}).get("content","") or "").strip()
    except Exception as e:
        logger.info("perception: Cerebras call failed: %s", e)
        return ""


def _parse_json_safely(text: str) -> Optional[dict]:
    """V17: aggressively rescue JSON from LLM output even when truncated.
    Pulls fields out manually if full parse fails."""
    if not text: return None
    # 1. Try direct parse
    try: return json.loads(text)
    except Exception: pass
    # 2. Try first {...} block (non-greedy)
    m = re.search(r"\{[\s\S]*?\}", text)
    if m:
        try: return json.loads(m.group(0))
        except Exception: pass
    # 3. Try first {...} block (greedy — for nested cases)
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try: return json.loads(m.group(0))
        except Exception: pass
    # 4. V17 RESCUE: manually pull the fields we care about, even if JSON broken
    rescued = {}
    for key in ("corrected", "is_garbage", "confidence", "clarify", "reasoning"):
        m = re.search(rf'"{key}"\s*:\s*("([^"\\]*(?:\\.[^"\\]*)*)"|true|false|[\d.]+)',
                      text)
        if m:
            v = m.group(1)
            if v.startswith('"'):
                rescued[key] = v[1:-1] if v.endswith('"') else v[1:]
            elif v in ("true", "false"):
                rescued[key] = (v == "true")
            else:
                try: rescued[key] = float(v)
                except Exception: pass
    if rescued.get("corrected"):
        return rescued
    return None


# ── Lightweight skip-conditions (don't waste an LLM call on trivial input) ──
_CHITCHAT_GREETING_RE = re.compile(
    r"^(?:hi+|hey+|hello+|yo|sup|good\s+(?:morning|evening|afternoon|night)|"
    r"how\s+(?:are\s+you|r\s+u|.{0,8}doing|.{0,8}going)|how.?s\s+it\s+going|"
    r"how.?s\s+(?:life|things)|what.?s\s+up|nice\s+to\s+meet|"
    r"thanks?|thank\s+you|appreciate)\b",
    re.I,
)


def _should_skip(text: str) -> bool:
    """Skip perception for trivially clean input — no need to burn tokens."""
    t = text.lower().strip()
    if len(t) < 3: return False                     # very short → DO perceive
    # Already very clean command-y inputs — let intent router try first
    if re.match(r"^(open|close|scroll|click|press|type|go to|switch to|"
                r"what's the weather|what time is it|tell me|hey maki)\b", t):
        return True
    # V17.2: greetings and chitchat — let agent's chitchat path handle them,
    # not the perception garbage detector
    if _CHITCHAT_GREETING_RE.match(t):
        return True
    return False


def perceive(transcript: str) -> Optional[Perception]:
    """Run the perception layer on a transcript. Returns None on hard failure
    (caller should fall back to the raw transcript)."""
    if not transcript or not transcript.strip(): return None

    # Fast skip — obviously clean commands don't need the LLM round-trip
    if _should_skip(transcript):
        return Perception(
            corrected_text=transcript.strip(),
            is_garbage=False, confidence=0.95,
            clarify="", reasoning="skip (clean command)",
            raw_input=transcript,
        )

    snap = ctx.snapshot()
    user_prompt = (
        f"CONTEXT:\n"
        f"- Foreground window title: {snap.get('window_title') or '(unknown)'}\n"
        f"- Last action performed: {snap.get('last_action') or '(none)'}\n"
        f"- Last user message: {snap.get('last_user_text') or '(none)'}\n"
        f"- Last assistant reply: {snap.get('last_assistant') or '(none)'}\n\n"
        f"NEW TRANSCRIPT: \"{transcript}\"\n\n"
        f"Reply with the JSON object only."
    )

    t0 = time.time()
    # V17: bumped from 220 to 450 to avoid mid-JSON truncation that caused
    # parse failures (see V16 log line 257).
    raw = _cerebras_call(user_prompt, max_tokens=450, timeout=8.0)
    dt_ms = int((time.time() - t0) * 1000)

    if not raw:
        logger.info("perception: no response from Cerebras (%dms) — using raw", dt_ms)
        return None

    data = _parse_json_safely(raw)
    if not data:
        logger.info("perception: couldn't parse JSON: %r — using raw", raw[:100])
        return None

    p = Perception(
        corrected_text = str(data.get("corrected", transcript)).strip() or transcript,
        is_garbage     = bool(data.get("is_garbage", False)),
        confidence     = float(data.get("confidence", 0.0) or 0.0),
        clarify        = str(data.get("clarify", "")).strip(),
        reasoning      = str(data.get("reasoning", "")).strip(),
        raw_input      = transcript,
    )
    logger.info("perception (%dms): %r → %r (conf %.2f, %s)",
                dt_ms, transcript, p.corrected_text, p.confidence,
                p.reasoning[:60])
    return p
