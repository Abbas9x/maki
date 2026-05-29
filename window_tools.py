"""
window_tools.py — V7 window control + better running-app detection.

Uses pygetwindow for cross-app enumeration and win32gui/win32con for
actions (minimize / maximize / restore / focus). Both ship via pywin32
which is already a project dep on Windows.

Public API:
  list_visible_windows()         → [{title, pid, exe}]
  list_running_apps()            → ['Chrome', 'Discord', 'Claude', ...]
  is_app_running(name)           → bool
  minimize_window(name)          → result dict
  maximize_window(name)          → result dict
  restore_window(name)           → result dict
  focus_window(name)             → result dict
"""

import logging, os, re

logger = logging.getLogger(__name__)

try:
    import psutil
except ImportError:
    psutil = None

try:
    import pygetwindow as gw
except Exception:
    gw = None

try:
    import win32gui, win32con, win32process
    _WIN32_OK = True
except Exception:
    _WIN32_OK = False


# ──────────────────────────────────────────────────────────────────────────────
# Known apps  → friendly label
# Extended (V7) to include Claude, ChatGPT, Cursor, Slack, Teams, Zoom, etc.
# ──────────────────────────────────────────────────────────────────────────────

_PROC_TO_LABEL = {
    # Browsers
    "chrome.exe":               "Chrome",
    "msedge.exe":               "Edge",
    "firefox.exe":              "Firefox",
    "brave.exe":                "Brave",
    "opera.exe":                "Opera",
    # Communication
    "discord.exe":              "Discord",
    "slack.exe":                "Slack",
    "teams.exe":                "Teams",
    "ms-teams.exe":             "Teams",
    "zoom.exe":                 "Zoom",
    "whatsapp.exe":             "WhatsApp",
    "telegram.exe":             "Telegram",
    # AI assistants
    "claude.exe":               "Claude",
    "chatgpt.exe":              "ChatGPT",
    "openai.exe":               "ChatGPT",
    # Media
    "spotify.exe":              "Spotify",
    "vlc.exe":                  "VLC",
    "obs64.exe":                "OBS",
    "obs.exe":                  "OBS",
    # Games / Riot
    "steam.exe":                "Steam",
    "riotclientservices.exe":   "Riot Client",
    "leagueclient.exe":         "League of Legends",
    "valorant.exe":             "VALORANT",
    "rocketleague.exe":         "Rocket League",
    "epicgameslauncher.exe":    "Epic Games",
    # Dev
    "code.exe":                 "VS Code",
    "cursor.exe":               "Cursor",
    "windsurf.exe":             "Windsurf",
    "pycharm64.exe":            "PyCharm",
    "idea64.exe":               "IntelliJ",
    "docker desktop.exe":       "Docker Desktop",
    "windowsterminal.exe":      "Windows Terminal",
    "wt.exe":                   "Windows Terminal",
    "powershell.exe":           "PowerShell",
    # Office
    "winword.exe":              "Word",
    "excel.exe":                "Excel",
    "powerpnt.exe":             "PowerPoint",
    "outlook.exe":              "Outlook",
    "onenote.exe":              "OneNote",
    "acrord32.exe":             "Acrobat Reader",
    # System
    "notepad.exe":              "Notepad",
    "taskmgr.exe":              "Task Manager",
    "explorer.exe":             "File Explorer",
    "calculator.exe":           "Calculator",
    "snippingtool.exe":         "Snipping Tool",
    "screensketch.exe":         "Snip & Sketch",
    "photoshop.exe":            "Photoshop",
    # Maki self (hide)
    "pythonw.exe":              None,   # hide
    "python.exe":               None,
}

# Common window-title hints for browser-based apps (Claude.ai web, ChatGPT web, Gmail)
# We use these when an app isn't a separate exe (it's a browser tab).
_TITLE_HINTS = [
    (re.compile(r"\bclaude\b",  re.I), "Claude (browser)"),
    (re.compile(r"chatgpt|chat\.openai", re.I), "ChatGPT (browser)"),
    (re.compile(r"gmail|google\s+mail", re.I), "Gmail (browser)"),
    (re.compile(r"youtube",     re.I), "YouTube (browser)"),
    (re.compile(r"github",      re.I), "GitHub (browser)"),
    (re.compile(r"reddit",      re.I), "Reddit (browser)"),
    (re.compile(r"twitter|x\.com", re.I), "Twitter/X (browser)"),
    (re.compile(r"notion",      re.I), "Notion (browser)"),
    (re.compile(r"linkedin",    re.I), "LinkedIn (browser)"),
]


# ──────────────────────────────────────────────────────────────────────────────
# Enumeration
# ──────────────────────────────────────────────────────────────────────────────

def list_visible_windows() -> list[dict]:
    """All visible top-level windows with non-empty titles."""
    if not _WIN32_OK:
        return []
    result = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title or len(title.strip()) < 2:
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            exe = ""
            if psutil:
                try:
                    exe = os.path.basename(psutil.Process(pid).name() or "").lower()
                except Exception:
                    pass
        except Exception:
            pid, exe = 0, ""
        result.append({"hwnd": hwnd, "title": title, "pid": pid, "exe": exe})

    try:
        win32gui.EnumWindows(cb, None)
    except Exception as e:
        logger.warning("EnumWindows failed: %s", e)
    return result


def list_running_apps() -> list[str]:
    """
    Friendly list of common apps that are *visible* right now.
    Combines (a) process scan for known exes (b) window-title heuristics
    for browser-based apps (Claude.ai, ChatGPT web, Gmail).
    """
    found: set[str] = set()
    # (a) Process-name scan
    if psutil:
        try:
            for p in psutil.process_iter(["name"]):
                exe = (p.info.get("name") or "").lower()
                label = _PROC_TO_LABEL.get(exe)
                if label:
                    found.add(label)
        except Exception:
            pass
    # (b) Window-title scan (browser-tab apps)
    for w in list_visible_windows():
        title = w["title"]
        for rx, label in _TITLE_HINTS:
            if rx.search(title):
                found.add(label)
                break
    return sorted(found)


def is_app_running(name: str) -> bool:
    """True if `name` matches any known running label or visible window title."""
    if not name:
        return False
    key = name.lower().strip()
    apps = [a.lower() for a in list_running_apps()]
    if any(key in a or a in key for a in apps):
        return True
    # Also look in window titles directly
    for w in list_visible_windows():
        if key in w["title"].lower():
            return True
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Actions: minimize / maximize / restore / focus
# ──────────────────────────────────────────────────────────────────────────────

def _find_hwnd_for(name: str) -> tuple[int, str] | None:
    """Return (hwnd, matched_title) of best window matching `name`.

    V14.5: previously "chrome" matched Discord because Discord is an
    Electron/Chromium app and some window title contained 'Chromium' or a
    dev-console flag. New priority:
      1. EXE name match (chrome.exe == chrome) — most reliable
      2. _PROC_TO_LABEL maps exe → friendly name → key match
      3. Title word-boundary match (not bare substring)
      4. Bare substring (last resort)
    """
    if not _WIN32_OK:
        return None
    key = name.lower().strip()
    if not key:
        return None
    wins = list_visible_windows()

    # 1. EXE name exact match — "chrome" → chrome.exe (skip discord.exe etc)
    for w in wins:
        exe = (w.get("exe") or "").lower()
        # exe stem (strip .exe)
        stem = exe[:-4] if exe.endswith(".exe") else exe
        if stem == key:
            return w["hwnd"], w["title"]

    # 2. PROC_TO_LABEL friendly name matches
    for w in wins:
        label = (_PROC_TO_LABEL.get(w.get("exe", ""), "") or "").lower()
        if label and (label == key or key == label.split()[0]):
            return w["hwnd"], w["title"]

    # 3. Title word-boundary match — "chrome" only matches words "chrome",
    #    not "chromium". Use \b for accuracy.
    word_re = re.compile(rf"\b{re.escape(key)}\b", re.I)
    for w in wins:
        if word_re.search(w["title"]):
            return w["hwnd"], w["title"]

    # 4. Last-resort substring — but ONLY if no other window is loosely
    #    related. Helps with custom titles like "MyProject - Chrome".
    for w in wins:
        if key in w["title"].lower() and key not in ("chrome","discord","spotify",
                                                      "edge","firefox","brave"):
            return w["hwnd"], w["title"]

    # 5. Exe substring (chrome → chrome.exe)
    for w in wins:
        if key in (w.get("exe") or ""):
            return w["hwnd"], w["title"]

    return None


def _do(name: str, action: str) -> dict:
    if not _WIN32_OK:
        return {"error": "Window control needs pywin32 (already installed). Restart Maki."}
    target = _find_hwnd_for(name)
    if not target:
        return {"error": f"I don't see any window for '{name}'."}
    hwnd, title = target
    cmd = {
        "minimize":  win32con.SW_MINIMIZE,
        "maximize":  win32con.SW_MAXIMIZE,
        "restore":   win32con.SW_RESTORE,
        "focus":     win32con.SW_RESTORE,   # restore + foreground
    }.get(action)
    if cmd is None:
        return {"error": f"Unknown window action '{action}'."}
    try:
        win32gui.ShowWindow(hwnd, cmd)
        if action == "focus":
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
        return {"ok": True, "title": title, "action": action}
    except Exception as e:
        return {"error": str(e), "title": title}


def minimize_window(name: str) -> dict:  return _do(name, "minimize")
def maximize_window(name: str) -> dict:  return _do(name, "maximize")
def restore_window(name: str)  -> dict:  return _do(name, "restore")
def focus_window(name: str)    -> dict:  return _do(name, "focus")
