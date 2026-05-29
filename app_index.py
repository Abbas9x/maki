"""
app_index.py — V8 adaptive app/process/window fuzzy resolver.

Instead of relying ONLY on hardcoded aliases, this builds a live, cached
index of launchable things on this PC:
  - Start Menu .lnk shortcuts (system + user)
  - currently running process names
  - currently open window titles

Plus a phonetic-alias table for common speech-to-text mishears
("clawed" -> claude, "dis chord" -> discord, "power shell" -> powershell).

Public API
----------
resolve(query) -> {
    "match":      canonical app name or None,
    "confidence": 0.0 .. 1.0,
    "candidates": [up to 3 close matches],
}
build_index(force=False) -> refreshes the cache (auto-refreshes every 2 min)
"""

from __future__ import annotations
import difflib, glob, logging, os, re, threading, time

logger = logging.getLogger(__name__)

# ── Phonetic / STT-mishear aliases → canonical app name ──────────────────────
# These map what the recognizer HEARS to what the user MEANT.
_ALIASES = {
    # Claude
    "claude": "claude", "clawed": "claude", "cloud": "claude",
    "clawed code": "claude", "claude code": "claude", "clod": "claude",
    "claud": "claude",
    # Discord
    "discord": "discord", "dis chord": "discord", "discored": "discord",
    "this cord": "discord", "the score": "discord",
    # PowerShell / terminals
    "powershell": "powershell", "power shell": "powershell",
    "windows powershell": "powershell", "windows power shell": "powershell",
    "rocklin powershell": "powershell", "rocking powershell": "powershell",
    "cmd": "cmd", "command prompt": "cmd",
    "terminal": "windows terminal", "windows terminal": "windows terminal",
    # Riot ecosystem
    "lol": "league of legends", "league": "league of legends",
    "league of legends": "league of legends", "leg": "league of legends",
    "riot": "riot client", "riot client": "riot client", "riot games": "riot client",
    "valorant": "valorant", "val": "valorant", "volaront": "valorant",
    # Browsers
    "google": "chrome", "google chrome": "chrome", "chrome": "chrome",
    "browser": "chrome", "internet": "chrome",
    "edge": "edge", "microsoft edge": "edge",
    "firefox": "firefox", "fire fox": "firefox",
    # Dev tools
    "vs code": "vs code", "vscode": "vs code", "visual studio code": "vs code",
    "code": "vs code", "code editor": "vs code", "the code": "vs code",
    "cursor": "cursor",
    # Media / chat
    "spotify": "spotify", "spotfy": "spotify", "spot if i": "spotify",
    "steam": "steam", "stem": "steam",
    "epic": "epic games", "epic games": "epic games",
    "discord ptb": "discord",
    # System
    "task manager": "task manager", "taskmgr": "task manager",
    "notepad": "notepad", "calculator": "calculator", "calc": "calculator",
    "nvidia": "nvidia", "geforce": "geforce experience",
    "explorer": "explorer", "file explorer": "explorer",
    "settings": "settings",
}

# Canonical names we never want fuzzy-overridden (anchors)
_CANONICAL = set(_ALIASES.values())

_index: dict[str, str] = {}      # normalized name → display name
_index_lock = threading.Lock()
_last_build = 0.0
_REFRESH_SECS = 120


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


# ── Index builders ───────────────────────────────────────────────────────────

def _scan_start_menu() -> dict[str, str]:
    dirs = [
        r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs",
        os.path.join(os.environ.get("APPDATA", ""),
                     r"Microsoft\Windows\Start Menu\Programs"),
    ]
    found: dict[str, str] = {}
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        try:
            for lnk in glob.glob(os.path.join(d, "**", "*.lnk"), recursive=True):
                base = os.path.splitext(os.path.basename(lnk))[0]
                nb = _norm(base)
                if nb and nb not in found:
                    found[nb] = base
        except Exception as e:
            logger.debug("Start Menu scan error in %s: %s", d, e)
    return found


def _scan_processes() -> dict[str, str]:
    found: dict[str, str] = {}
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            n = (p.info.get("name") or "").lower()
            if n.endswith(".exe"):
                base = n[:-4]
                nb = _norm(base)
                if nb and len(nb) > 1:
                    found.setdefault(nb, base.title())
    except Exception as e:
        logger.debug("Process scan error: %s", e)
    return found


def _scan_windows() -> dict[str, str]:
    found: dict[str, str] = {}
    try:
        import window_tools
        for w in window_tools.list_visible_windows():
            t = (w.get("title") or "").strip()
            if t and len(t) > 2:
                nt = _norm(t)[:48]
                if nt:
                    found.setdefault(nt, t[:48])
    except Exception as e:
        logger.debug("Window scan error: %s", e)
    return found


def build_index(force: bool = False) -> None:
    global _last_build
    now = time.time()
    if not force and _index and (now - _last_build) < _REFRESH_SECS:
        return
    idx: dict[str, str] = {}
    idx.update(_scan_start_menu())
    for k, v in _scan_processes().items():
        idx.setdefault(k, v)
    for k, v in _scan_windows().items():
        idx.setdefault(k, v)
    with _index_lock:
        _index.clear()
        _index.update(idx)
    _last_build = now
    logger.info("app_index: built %d entries", len(idx))


# ── Resolver ─────────────────────────────────────────────────────────────────

def resolve(query: str) -> dict:
    """
    Resolve a (possibly misheard) app name. Returns:
        {"match": str|None, "confidence": float, "candidates": [str, ...]}
    confidence guide:
        >= 0.90  use it directly
        0.60-0.89 ask "did you mean ...?"
        < 0.60   no good match
    """
    q = _norm(query)
    if not q:
        return {"match": None, "confidence": 0.0, "candidates": []}

    # 1. Exact alias hit
    if q in _ALIASES:
        return {"match": _ALIASES[q], "confidence": 1.0, "candidates": [_ALIASES[q]]}

    # 2. Alias substring (handles "open the discord please" → "discord")
    for alias, canon in _ALIASES.items():
        if alias in q.split() or (len(alias) > 3 and alias in q):
            return {"match": canon, "confidence": 0.93, "candidates": [canon]}

    # 3. Fuzzy against alias keys (catches mild mishears not in the table)
    alias_keys = list(_ALIASES.keys())
    am = difflib.get_close_matches(q, alias_keys, n=3, cutoff=0.7)
    if am:
        ratio = difflib.SequenceMatcher(None, q, am[0]).ratio()
        cands = []
        for m in am:
            c = _ALIASES[m]
            if c not in cands:
                cands.append(c)
        return {"match": cands[0], "confidence": round(ratio, 2), "candidates": cands}

    # 4. Fuzzy against the live index (Start Menu / processes / windows)
    build_index()
    with _index_lock:
        names = list(_index.keys())
    im = difflib.get_close_matches(q, names, n=3, cutoff=0.6)
    if im:
        with _index_lock:
            cands = []
            for m in im:
                d = _index.get(m, m)
                if d not in cands:
                    cands.append(d)
        ratio = difflib.SequenceMatcher(None, q, im[0]).ratio()
        return {"match": cands[0], "confidence": round(ratio, 2), "candidates": cands}

    return {"match": None, "confidence": 0.0, "candidates": []}
