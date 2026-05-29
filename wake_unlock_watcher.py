"""
wake_unlock_watcher.py — V7 wake / unlock greeting watcher.

Runs persistently inside Maki (started by main.py on boot). Polls the
Windows lock state and time delta between checks. When it detects:
  • A return from lock screen (screen was locked, now unlocked), OR
  • A long gap between polls (system was asleep/hibernated),
it triggers a callback so Maki can greet the user naturally.

We use Win32 OpenInputDesktop trick (reliable for lock detection on a
normal user session) plus a wall-clock-gap detector for sleep/resume.

Limitations (honest):
  • Wake-from-sleep is detected by the wall-clock gap heuristic. Windows
    does NOT fire a clean user-space event for resume in all configs, so
    a gap of > 90 s between polls is the most reliable signal.
  • If the watcher itself is killed by sleep, it resumes correctly when
    the process is rescheduled (Python loop ticks again).
"""

import logging, threading, time

logger = logging.getLogger(__name__)

try:
    import ctypes
    from ctypes import wintypes
    _USER32 = ctypes.windll.user32
    _WIN32_OK = True
except Exception:
    _WIN32_OK = False


def _is_workstation_locked() -> bool:
    """
    Returns True if the workstation is currently locked.
    Trick: OpenInputDesktop fails when locked.
    """
    if not _WIN32_OK:
        return False
    try:
        hdesk = _USER32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if hdesk == 0:
            return True
        _USER32.CloseDesktop(hdesk)
        return False
    except Exception:
        return False


class WakeUnlockWatcher:
    """
    Calls `on_event(kind)` where kind ∈ {'unlock', 'wake'}.

    Start it once from main.py:
        watcher = WakeUnlockWatcher(callback)
        watcher.start()
    """

    POLL_SECONDS    = 5         # check cadence
    SLEEP_GAP_THRES = 90        # wall-clock gap → likely woke from sleep

    def __init__(self, on_event):
        self._on_event = on_event
        self._stop     = threading.Event()
        self._thread   = None

    def start(self):
        if not _WIN32_OK:
            logger.info("WakeUnlockWatcher: pywin32 unavailable — disabled.")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="wake-unlock-watcher",
        )
        self._thread.start()
        logger.info("WakeUnlockWatcher started.")

    def stop(self):
        self._stop.set()

    def _loop(self):
        was_locked   = _is_workstation_locked()
        last_tick    = time.monotonic()
        while not self._stop.is_set():
            time.sleep(self.POLL_SECONDS)
            now      = time.monotonic()
            gap      = now - last_tick
            last_tick = now

            # ── Wake / resume from sleep ─────────────────────────────────────
            # If the gap is much larger than our poll period, we were asleep.
            if gap > self.SLEEP_GAP_THRES:
                logger.info("Detected wake-from-sleep (gap=%.0fs)", gap)
                self._fire("wake")
                # After resume, also check lock state
                was_locked = _is_workstation_locked()
                continue

            # ── Unlock detection ─────────────────────────────────────────────
            try:
                locked = _is_workstation_locked()
            except Exception:
                locked = was_locked
            if was_locked and not locked:
                logger.info("Detected unlock.")
                self._fire("unlock")
            was_locked = locked

    def _fire(self, kind: str):
        try:
            self._on_event(kind)
        except Exception as e:
            logger.warning("WakeUnlock callback failed: %s", e)
