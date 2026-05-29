"""
lane_classifier.py — V19 Step 4

Maps an incoming utterance to one of the 6 brain lanes. Used by the brain
router BEFORE deciding which provider to call.

Lanes:
  github_premium   — Think toggle ON (deep reasoning, frontier model)
  hermes_tools     — Tool-call intents (function-calling on Hermes 3 local)
  groq_8b          — Casual chat (social / greetings / jokes)
  cerebras_120b    — Knowledge / explanations / reasoning (default brain)
  nim_nemotron     — Overflow (Cerebras 8K-exceeded or quota hits)
  vision           — Screenshot / screen perception

Selection rules (in priority order):
  1. think_mode_on  → github_premium (unless tool-call intent, which always wins)
  2. Tool-call intent classified above confidence threshold → hermes_tools
  3. Follow-up of recent turn within 5 min, new intent conf < 0.6 → inherit parent lane
  4. Intent difficulty tag → lane
  5. Default → cerebras_120b

The intent → difficulty map covers the V14.5 intents. New intents added later
default to "knowledge" (safe middle ground) until a tag is added.
"""

from __future__ import annotations
import logging, time

logger = logging.getLogger(__name__)

# ── difficulty tags per known intent (V14.5 intent set) ──────────────────────
# social    → small, fast model (Groq 8B)
# knowledge → default brain (Cerebras 120B)
# tool      → Hermes 3 local function-calling (HARD override)
# vision    → vision lane (qwen3-vl:4b + Gemini fallback)
# deep      → reserved for think-mode-only intents (rare)
INTENT_DIFFICULTY: dict[str, str] = {
    # Tool / action intents
    "open_app":         "tool",
    "close_app":        "tool",
    "focus_app":        "tool",
    "minimize":         "tool",
    "maximize":         "tool",
    "click_center":     "tool",
    "click_element":    "tool",
    "copy":             "tool",
    "paste":            "tool",
    "cut":              "tool",
    "back":             "tool",
    "forward":          "tool",
    "refresh":          "tool",
    "new_tab":          "tool",
    "close_tab":        "tool",
    "reopen_tab":       "tool",
    "redo":             "tool",
    "clear_field":      "tool",
    # Knowledge / fast-info intents (handled locally — but if missed, lane defaults sensibly)
    "get_time":         "knowledge",
    "get_date":         "knowledge",
    "get_weather":      "knowledge",
    # Vision intents
    "describe_screen":  "vision",
    "look_at_screen":   "vision",
    "read_screen":      "vision",
    "screenshot":       "vision",
}

# Casual-chat keywords that route to the social lane even when no intent matches.
# Includes polite dismissals ("never mind", "forget it") — these are too short
# and conversational to spend Cerebras tokens on.
_SOCIAL_TRIGGERS = (
    "joke", "lol", "haha", "how are you", "what's up", "whats up",
    "thanks", "thank you", "good morning", "good night", "hello",
    "hi maki", "hey maki", "sup", "yo maki",
    "never mind", "nevermind", "forget it", "no thanks", "no thank you",
)

# V19 BUG-4b FIX: Absolute social overrides — these utterances must NEVER
# route to hermes_tools regardless of intent classification confidence.
# "thank you" was getting matched to a tool intent and triggering keypress.
# Checked via _is_hard_social() BEFORE the tool-call override.
_HARD_SOCIAL_OVERRIDES = frozenset({
    "thank you", "thanks", "thank you very much", "thanks a lot",
    "appreciate it", "much appreciated", "cheers",
    "no worries", "that's fine", "thats fine", "all good", "no problem",
    "okay", "ok", "alright", "got it", "sure", "yep", "yeah", "yup", "nope",
    "nice", "great", "awesome", "perfect", "good", "cool", "sweet",
    "oh", "ah", "uh", "hmm", "wow", "oh my god", "omg",
})

def _is_hard_social(text: str) -> bool:
    """True if utterance is a pure social/acknowledgement that must never
    become a tool call. Match the whole stripped phrase, not substring —
    so "thank you for opening chrome" doesn't trigger."""
    t = (text or "").strip().lower().strip(".,!?")
    return t in _HARD_SOCIAL_OVERRIDES

# Follow-up cue words — utterances that lean on previous-turn context
_FOLLOWUP_CUES = (
    "simpler", "shorter", "longer", "more", "again", "another",
    "wait", "actually", "no the", "yes the", "that one", "this one",
    "the other", "explain more", "go on", "continue",
)

# Strong follow-up phrases — inherit parent lane regardless of intent conf,
# because raw embedding similarity gives spurious 0.7+ scores on short input.
_STRONG_FOLLOWUP = (
    "simpler", "simply", "shorter", "go on", "continue", "explain more",
    "explain that", "actually wait", "no wait", "keep going", "elaborate",
    "do it", "do that", "yes do", "yeah do", "sure go",
    "more detail", "less detail", "again",
)

CONF_TOOL_FLOOR    = 0.78   # stricter — raw embedding cosine on 'hi maki'
                            # vs 'paste' hits ~0.65, so we need to be safely above noise
# V19 BUG-3b refinement: two thresholds for two purposes —
#   CONF_BREAK_INHERIT = 0.50: any-intent-match above this stops inheritance
#                              (a new topic was clearly identified)
#   CONF_ACT_ON_INTENT = 0.70: minimum confidence to actually ROUTE based on
#                              the intent (vision/tool lanes require it)
CONF_BREAK_INHERIT = 0.50
CONF_ACT_ON_INTENT = 0.70
# Legacy alias used in earlier code paths
CONF_NEW_INTENT    = CONF_ACT_ON_INTENT
INHERIT_WINDOW_S   = 60     # V19 BUG-3b: was 300 — tool/info answers are
                            # one-shot, not conversations. 60s is plenty for
                            # "simpler please" / "go on" follow-ups.

# V19 BUG-B FIX: Vision-perception triggers — utterances that MUST go to the
# vision lane, even if the intent router falsely classifies them as a tool
# call (e.g. "what can I click on" → click_element @ 0.85 → hermes_tools).
# Screen-perception beats tool-override.
import re as _re
VISION_PERCEPTION_TRIGGERS = _re.compile(
    r"\b("
    r"what(?:'?s| is| are)?\s+(?:on|in)\s+(?:my\s+)?screen"
    r"|what\s+do\s+you\s+see"
    r"|what\s+can\s+you\s+see"
    r"|what\s+app\s+(?:am|is)\s+i"
    r"|what\s+am\s+i\s+(?:looking|seeing|on)"
    r"|what\s+can\s+i\s+click"
    r"|what'?s\s+clickable"
    r"|describe\s+(?:my\s+)?screen"
    r"|what'?s\s+visible"
    r"|what\s+do\s+i\s+have\s+open"
    r"|what'?s\s+in\s+front\s+of\s+me"
    r"|can\s+i\s+show\s+you"
    r"|look\s+at\s+(?:my\s+)?screen"
    r"|what(?:'?s| is)\s+there"
    r"|what\s+do\s+you\s+see\s+on"
    r")\b",
    _re.I,
)

# V19 BUG-C FIX: Only chat lanes can be inherited by follow-up turns. Tool
# lanes must NEVER carry over — "ready for" after "pressed alt+right" must
# not re-fire alt+right. Vision/tool lanes restart fresh on each turn.
INHERITABLE_LANES = {"groq_8b", "cerebras_120b", "github_premium", "nim_nemotron"}


# ── conversation lane memory ─────────────────────────────────────────────────
_last_lane:       str | None = None
_last_lane_t:     float       = 0.0
_last_text:       str         = ""     # V19 BUG-3b: for topic-noun overlap check


def remember_lane(lane: str, utterance: str = "") -> None:
    """Brain calls this after each turn so the next turn can inherit."""
    global _last_lane, _last_lane_t, _last_text
    _last_lane    = lane
    _last_lane_t  = time.time()
    _last_text    = utterance or ""


# V19 BUG-3b: topic-change detector. If the new utterance shares NO content
# words with the previous one, treat it as a fresh turn (don't inherit).
# Stopwords list is intentionally short — we want strong topic-overlap
# requirements, not loose ones.
_STOPWORDS = {
    "a","an","the","is","are","was","were","be","been","being","do","does",
    "did","done","have","has","had","having","i","you","we","they","he",
    "she","it","me","my","your","our","their","this","that","these","those",
    "and","or","but","if","then","so","for","of","to","in","on","at","by",
    "with","from","as","what","when","where","why","how","which","who",
    "whose","whom","please","just","very","more","also","too","really",
    "any","some","much","many","tell","say","show","give","make","do",
    "going","get","got","go",
}


def _content_words(text: str) -> set[str]:
    t = (text or "").lower()
    t = _re.sub(r"[^\w\s]", " ", t)
    return {w for w in t.split() if len(w) > 2 and w not in _STOPWORDS}


def _shares_topic_with_last(text: str) -> bool:
    """True if current utterance shares at least one content word with the
    previous one. Used to decide whether inheritance is even reasonable."""
    cur = _content_words(text)
    prev = _content_words(_last_text)
    if not cur or not prev:
        return False
    return bool(cur & prev)


def _is_followup(text: str) -> bool:
    t = (text or "").lower().strip()
    if not t: return False
    if len(t.split()) <= 3:   # very short utterances are usually follow-ups
        return True
    return any(cue in t for cue in _FOLLOWUP_CUES)


def _is_social(text: str) -> bool:
    t = (text or "").lower().strip()
    return any(s in t for s in _SOCIAL_TRIGGERS)


# ── main entry ───────────────────────────────────────────────────────────────
def select_lane(text: str,
                think_mode_on: bool = False,
                router=None) -> tuple[str, dict]:
    """
    Returns (lane_name, decision_info).

    `router` is the IntentRouter instance (from intents.build_router()).
    `decision_info` is a dict suitable for logging — includes intent name,
    confidence, reason for choice, etc.
    """
    info: dict = {
        "text":           (text or "")[:120],
        "think_mode":     think_mode_on,
        "intent":         None,
        "intent_conf":    None,
        "difficulty":     None,
        "lane":           None,
        "reason":         None,
        "inherited":      False,
    }

    # Step A: classify intent (if router available)
    intent_name, conf = None, 0.0
    if router is not None:
        try:
            r = router.classify(text)
            if r is not None:
                intent_name, conf = r
        except Exception as e:
            logger.info("lane_classifier: router.classify failed: %s", e)
    info["intent"]      = intent_name
    info["intent_conf"] = round(conf, 3) if intent_name else None
    if intent_name:
        info["difficulty"] = INTENT_DIFFICULTY.get(intent_name, "knowledge")

    # Step A1: HARD SOCIAL OVERRIDE (V19 BUG-4b FIX).
    # "thank you" / "okay" / "got it" must never become a tool call,
    # regardless of intent confidence. Runs FIRST so it beats every other
    # override including perception.
    if _is_hard_social(text):
        info["lane"]   = "groq_8b"
        info["reason"] = "hard_social_override"
        return info["lane"], info

    # Step A2: VISION PERCEPTION OVERRIDE (V19 BUG-B FIX).
    # Force vision lane for screen-question phrases BEFORE social or tool
    # checks. "what can I click on" → vision, even though intent router
    # matches click_element with high confidence.
    if VISION_PERCEPTION_TRIGGERS.search(text or ""):
        info["lane"]   = "vision"
        info["reason"] = "perception_keyword_override"
        return info["lane"], info

    # Step B: Social keywords → Groq 8B (FIRST, before noisy intent match
    # can produce a false tool override on greetings like "hi maki").
    if _is_social(text):
        info["lane"]   = "groq_8b"
        info["reason"] = "social_keyword"
        return info["lane"], info

    # Step C: Follow-up inheritance. Two paths:
    #  (a) STRONG cue ("simpler", "go on", ...) — inherit regardless of conf
    #      (raw embedding gives spurious 0.7+ on short input)
    #  (b) Weak cue / short utterance + conf below CONF_NEW_INTENT
    #
    # V19 BUG-C FIX: hermes_tools and vision are NEVER inheritable.
    # "ready for" after "pressed alt+right" must not re-fire alt+right.
    now = time.time()
    t_lower = (text or "").lower()
    in_window = (_last_lane is not None
                 and (now - _last_lane_t) <= INHERIT_WINDOW_S
                 and _last_lane in INHERITABLE_LANES)
    if in_window and any(s in t_lower for s in _STRONG_FOLLOWUP):
        info["lane"]      = _last_lane
        info["reason"]    = "followup_strong_cue"
        info["inherited"] = True
        return info["lane"], info
    # V19 BUG-3b: weak-cue inheritance uses the LOWER conf threshold
    # (BREAK_INHERIT=0.50) — any strong-ish new intent breaks the chain.
    # Topic-overlap is required IF we have a previous utterance to compare to.
    # If _last_text was never set (legacy callers), skip overlap check and
    # use the old weak-cue behavior.
    if in_window and conf < CONF_BREAK_INHERIT and _is_followup(text):
        topic_ok = (not _last_text) or _shares_topic_with_last(text)
        if topic_ok:
            info["lane"]      = _last_lane
            info["reason"]    = "followup_inherit"
            info["inherited"] = True
            return info["lane"], info

    # Step D: Tool-call intent (strict floor — embedding noise hits ~0.65,
    # so CONF_TOOL_FLOOR=0.78 keeps non-tool utterances from accidentally
    # routing to Hermes function-calling).
    if intent_name and conf >= CONF_TOOL_FLOOR and INTENT_DIFFICULTY.get(intent_name) == "tool":
        info["lane"]   = "hermes_tools"
        info["reason"] = "tool_intent_override"
        return info["lane"], info

    # Step E: Vision intent (above confidence) → vision lane
    if intent_name and conf >= CONF_NEW_INTENT and INTENT_DIFFICULTY.get(intent_name) == "vision":
        info["lane"]   = "vision"
        info["reason"] = "vision_intent"
        return info["lane"], info

    # Step F: Think mode ON → premium lane (after tool-call override)
    if think_mode_on:
        info["lane"]   = "github_premium"
        info["reason"] = "think_mode_on"
        return info["lane"], info

    # Step G: Map difficulty tag to lane (when we have a confident intent)
    if intent_name and conf >= CONF_NEW_INTENT:
        d = INTENT_DIFFICULTY.get(intent_name, "knowledge")
        if d == "social":
            info["lane"]   = "groq_8b"
            info["reason"] = "intent_difficulty_social"
            return info["lane"], info
        if d == "deep":
            info["lane"]   = "github_premium"
            info["reason"] = "intent_difficulty_deep"
            return info["lane"], info
        if d == "vision":
            info["lane"]   = "vision"
            info["reason"] = "intent_difficulty_vision"
            return info["lane"], info
        # knowledge / tool (low conf tool) → default
        info["lane"]   = "cerebras_120b"
        info["reason"] = "intent_difficulty_knowledge"
        return info["lane"], info

    # Step H: default
    info["lane"]   = "cerebras_120b"
    info["reason"] = "default"
    return info["lane"], info


# ── audit log for routing decisions ─────────────────────────────────────────
def log_decision(info: dict) -> None:
    """Append-only routing decision log so we can grep 'why did Maki use lane X?'"""
    try:
        import json, os, threading
        from pathlib import Path
        log = Path(__file__).parent / "logs" / "v19_routing.jsonl"
        log.parent.mkdir(exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            info_with_ts = dict(info)
            info_with_ts["ts"] = time.time()
            f.write(json.dumps(info_with_ts, ensure_ascii=False) + "\n")
    except Exception:
        pass
