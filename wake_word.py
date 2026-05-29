"""
wake_word.py - Wake phrase detection with one-breath command support.
If user says "hey maki open spotify" in one phrase, we extract "open spotify"
and pass it directly — no second listen needed.
"""

import logging, threading, re
import config, speech

logger = logging.getLogger(__name__)

WAKE_WORDS  = set(config.WAKE_SINGLE_WORDS)
WAKE_ALTS   = config.WAKE_ALTERNATIVES


def _is_wake(text: str) -> bool:
    if not text: return False
    t = text.lower().strip()
    if any(a in t for a in WAKE_ALTS):          return True
    words = t.split()
    if len(words) <= 2 and any(w in words for w in WAKE_WORDS): return True
    # "hey" + any M/K word covers all mishearings of Maki
    if len(words) >= 2 and words[0] == "hey" and words[1][0] in "mk": return True
    if t == "hey":                               return True
    return False


def extract_inline_command(text: str) -> str | None:
    """
    If text is "hey maki open spotify", return "open spotify".
    Returns None if no command follows the wake phrase.
    """
    t = text.lower().strip()
    words = t.split()

    # Remove "hey [m/k-word]" prefix
    if len(words) >= 2 and words[0] == "hey" and words[1][0] in "mk":
        remainder = " ".join(words[2:]).strip()
        return remainder if remainder else None

    # Remove any known alternative prefix
    for alt in sorted(WAKE_ALTS, key=len, reverse=True):
        if t.startswith(alt):
            remainder = t[len(alt):].strip()
            return remainder if remainder else None

    return None


class WakeWordListener:
    def __init__(self, on_wake, on_raw_text=None):
        """
        on_wake(text) — called with the full heard text.
        on_raw_text(text) — called for every phrase, for GUI display.
        """
        self._on_wake = on_wake
        self._on_raw  = on_raw_text
        self._running = False
        self._pause   = threading.Event()
        self._pause.set()
        self._thread  = None

    def start(self):
        if self._running: return
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Wake listener started. Phrase: '%s'", config.WAKE_PHRASE)

    def stop(self):
        self._running = False
        self._pause.set()

    def pause(self):  self._pause.clear()
    def resume(self): self._pause.set()

    def _loop(self):
        while self._running:
            self._pause.wait()
            if not self._running: break

            text = speech.listen_once(timeout=4.0)
            if not text: continue

            logger.info("Heard: '%s'", text)
            if self._on_raw:
                self._on_raw(text)

            if _is_wake(text):
                logger.info("Wake matched on: '%s'", text)
                self._on_wake(text)   # pass full text for inline command extraction
