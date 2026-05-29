"""
tools.py - Ground-truth Python tools for Maki V2.

These tools return FACTS — never invented by Ollama.
Ollama reasons about WHAT to call; Python produces the actual answer.
"""

import datetime, logging, os, shutil, webbrowser, subprocess, re
from zoneinfo import ZoneInfo
import config, actions

logger = logging.getLogger(__name__)

# ── Timezone map (same as V1 actions.py) ──────────────────────────────────────
_TZ = {
    "pakistan":"Asia/Karachi","karachi":"Asia/Karachi",
    "islamabad":"Asia/Karachi","lahore":"Asia/Karachi","peshawar":"Asia/Karachi",
    "dubai":"Asia/Dubai","uae":"Asia/Dubai","abu dhabi":"Asia/Dubai",
    "saudi":"Asia/Riyadh","riyadh":"Asia/Riyadh","jeddah":"Asia/Riyadh",
    "turkey":"Europe/Istanbul","istanbul":"Europe/Istanbul","ankara":"Europe/Istanbul",
    "uk":"Europe/London","london":"Europe/London","england":"Europe/London",
    "india":"Asia/Kolkata","mumbai":"Asia/Kolkata","delhi":"Asia/Kolkata",
    "new york":"America/New_York","nyc":"America/New_York","est":"America/New_York",
    "los angeles":"America/Los_Angeles","la":"America/Los_Angeles","pst":"America/Los_Angeles",
    "chicago":"America/Chicago","texas":"America/Chicago","dallas":"America/Chicago",
    "germany":"Europe/Berlin","berlin":"Europe/Berlin",
    "france":"Europe/Paris","paris":"Europe/Paris",
    "japan":"Asia/Tokyo","tokyo":"Asia/Tokyo",
    "china":"Asia/Shanghai","beijing":"Asia/Shanghai","shanghai":"Asia/Shanghai",
    "australia":"Australia/Sydney","sydney":"Australia/Sydney",
    "canada":"America/Toronto","toronto":"America/Toronto",
    "vancouver":"America/Vancouver","calgary":"America/Edmonton",
    "edmonton":"America/Edmonton","montreal":"America/Toronto",
    "ottawa":"America/Toronto","winnipeg":"America/Winnipeg",
    "houston":"America/Chicago","denver":"America/Denver",
    "phoenix":"America/Phoenix","miami":"America/New_York",
    "seattle":"America/Los_Angeles","san francisco":"America/Los_Angeles",
    "boston":"America/New_York","atlanta":"America/New_York",
    "moscow":"Europe/Moscow","russia":"Europe/Moscow",
    "brazil":"America/Sao_Paulo","sao paulo":"America/Sao_Paulo",
    "mexico":"America/Mexico_City","mexico city":"America/Mexico_City",
    "singapore":"Asia/Singapore","malaysia":"Asia/Kuala_Lumpur",
    "kuala lumpur":"Asia/Kuala_Lumpur","bangkok":"Asia/Bangkok",
    "thailand":"Asia/Bangkok",
}

_DAYS = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]


# ── Time & Date ───────────────────────────────────────────────────────────────

def get_current_time() -> str:
    now  = datetime.datetime.now()
    h    = now.strftime("%I").lstrip("0") or "12"
    m    = now.strftime("%M")
    ampm = now.strftime("%p")
    return f"{h}:{m} {ampm}" if m != "00" else f"{h} {ampm}"


def add_time_offset(hours: int = 0, minutes: int = 0) -> str:
    """Return the clock time after adding hours/minutes to the current time."""
    future = datetime.datetime.now() + datetime.timedelta(hours=hours, minutes=minutes)
    h    = future.strftime("%I").lstrip("0") or "12"
    m    = future.strftime("%M")
    ampm = future.strftime("%p")
    return f"{h}:{m} {ampm}" if m != "00" else f"{h} {ampm}"


def get_current_date() -> str:
    now    = datetime.datetime.now()
    day    = now.day
    suffix = "th" if 11 <= day <= 13 else {1:"st",2:"nd",3:"rd"}.get(day % 10, "th")
    return f"{now.strftime('%A, %B')} {day}{suffix}, {now.year}"


def get_time_in(location: str) -> dict:
    loc     = location.lower().strip()
    tz_name = _TZ.get(loc)
    if not tz_name:
        for key, tz in _TZ.items():
            if key in loc or loc in key:
                tz_name = tz
                break
    if not tz_name:
        return {"error": f"Unknown timezone for '{location}'"}
    try:
        now  = datetime.datetime.now(ZoneInfo(tz_name))
        h    = now.strftime("%I").lstrip("0") or "12"
        m    = now.strftime("%M")
        ampm = now.strftime("%p")
        t    = f"{h}:{m} {ampm}" if m != "00" else f"{h} {ampm}"
        return {"time": t, "location": location.title(), "timezone": tz_name}
    except Exception as e:
        return {"error": str(e)}


def calculate_day_of_date(date_str: str) -> dict:
    """
    Robustly parse a spoken/typed date and return the day of the week.

    Handles: 'May 27', '27th May', '27th of May', 'May 27th 2026',
             '2026-05-27', '27/5', 'it on 27th May', 'the 27th of May'.
    Assumes current year when year is absent.
    """
    raw = date_str.strip()
    now = datetime.datetime.now()
    assumed_year = False
    dt = None

    # ── Pre-clean: remove leading noise from STT/brain extraction ────────────
    # e.g. "it on", "it in", "on the", "the"
    s = re.sub(r"^(it\s+)?(on|in|for|the)\s+(the\s+)?", "", raw, flags=re.I).strip()
    # "Nth of Month" → "Month Nth"  (e.g. "27th of May" → "May 27th")
    s = re.sub(r"(\d+(?:st|nd|rd|th)?)\s+of\s+(\w+)", r"\2 \1", s, flags=re.I)
    # "Month Nth of year" edge case handled by formats below
    # Strip ordinal suffixes: "27th" → "27", "1st" → "1"
    s_clean = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", s, flags=re.I).strip()

    # ── Try with explicit year ────────────────────────────────────────────────
    for src in (s, s_clean):
        for fmt in ("%B %d %Y", "%b %d %Y", "%Y-%m-%d",
                    "%m/%d/%Y", "%d/%m/%Y", "%d %B %Y", "%d %b %Y"):
            try:
                dt = datetime.datetime.strptime(src.strip(), fmt)
                break
            except ValueError:
                continue
        if dt:
            break

    # ── Try without year (assume current) ────────────────────────────────────
    if dt is None:
        for src in (s_clean, s):
            for fmt in ("%B %d", "%b %d", "%d %B", "%d %b", "%m/%d", "%d/%m"):
                try:
                    parsed = datetime.datetime.strptime(src.strip(), fmt)
                    dt = parsed.replace(year=now.year)
                    assumed_year = True
                    break
                except ValueError:
                    continue
            if dt:
                break

    if dt is None:
        return {"error": f"Could not parse date from: '{date_str}'"}

    day_name  = _DAYS[dt.weekday()]
    suffix    = "th" if 11 <= dt.day <= 13 else {1:"st",2:"nd",3:"rd"}.get(dt.day % 10, "th")
    formatted = f"{dt.strftime('%B')} {dt.day}{suffix}, {dt.year}"
    return {
        "day_of_week":  day_name,
        "date":         formatted,
        "assumed_year": assumed_year,
        "year":         dt.year,
    }


# ── Folder size (V7.5) ────────────────────────────────────────────────────────

_FOLDER_ALIASES = {
    "projectmaki":         os.path.join(config.BASE_USER_FOLDER, "projectmaki"),
    "project maki":        os.path.join(config.BASE_USER_FOLDER, "projectmaki"),
    "maki":                os.path.join(config.BASE_USER_FOLDER, "projectmaki"),
    "maki folder":         os.path.join(config.BASE_USER_FOLDER, "projectmaki"),
    "this folder":         os.path.join(config.BASE_USER_FOLDER, "projectmaki"),
    "screenshots":         os.path.join(config.BASE_USER_FOLDER, "Pictures", "MakiScreenshots"),
    "screenshots folder":  os.path.join(config.BASE_USER_FOLDER, "Pictures", "MakiScreenshots"),
    "maki screenshots":    os.path.join(config.BASE_USER_FOLDER, "Pictures", "MakiScreenshots"),
    "downloads":           config.DOWNLOADS_FOLDER,
    "downloads folder":    config.DOWNLOADS_FOLDER,
    "documents":           config.DOCUMENTS_FOLDER,
    "documents folder":    config.DOCUMENTS_FOLDER,
    "n8n":                 config.N8N_PROJECTS_FOLDER,
    "n8n folder":          config.N8N_PROJECTS_FOLDER,
}


def _resolve_folder(name_or_path: str) -> str | None:
    if not name_or_path:
        return None
    key = name_or_path.lower().strip().rstrip("/\\")
    # Direct path?
    if os.path.isdir(key):
        return key
    # Alias
    if key in _FOLDER_ALIASES:
        return _FOLDER_ALIASES[key]
    # Strip "folder" suffix
    for alias, path in _FOLDER_ALIASES.items():
        if alias in key or key in alias:
            return path
    return None


def get_folder_size(name_or_path: str) -> dict:
    """Recursive on-disk size of a folder. Returns size in MB/GB friendly format."""
    path = _resolve_folder(name_or_path)
    if path is None:
        return {"error": f"I don't know a folder called '{name_or_path}'."}
    if not os.path.isdir(path):
        return {"error": f"Folder not found: {path}"}
    total = 0
    files = 0
    try:
        for root, dirs, fnames in os.walk(path):
            for fn in fnames:
                full = os.path.join(root, fn)
                try:
                    total += os.path.getsize(full)
                    files += 1
                except OSError:
                    continue
    except Exception as e:
        return {"error": f"Couldn't measure '{path}': {e}"}
    # Human-readable
    if total >= 1024 ** 3:
        size = f"{total / (1024**3):.2f} GB"
    elif total >= 1024 ** 2:
        size = f"{total / (1024**2):.1f} MB"
    else:
        size = f"{total / 1024:.0f} KB"
    return {"path": path, "bytes": total, "size": size, "files": files}


# ── Game / app install-size scanner (V8) ─────────────────────────────────────

# Common launcher library roots — checked across C: and D:
_GAME_LIBRARY_ROOTS = [
    r"C:\Program Files (x86)\Steam\steamapps\common",
    r"D:\Steam\steamapps\common",
    r"D:\SteamLibrary\steamapps\common",
    r"C:\Program Files\Epic Games",
    r"D:\Epic Games",
    r"C:\Riot Games",
    r"D:\Riot Games",
    r"C:\Program Files\Riot Games",
]

# Direct known game/app folders (fast path — no scanning of huge trees)
_KNOWN_GAME_DIRS = {
    "league of legends": [r"C:\Riot Games\League of Legends",
                          r"D:\Riot Games\League of Legends"],
    "league":            [r"C:\Riot Games\League of Legends",
                          r"D:\Riot Games\League of Legends"],
    "valorant":          [r"C:\Riot Games\VALORANT",
                          r"D:\Riot Games\VALORANT"],
    "riot client":       [r"C:\Riot Games", r"D:\Riot Games"],
    "riot":              [r"C:\Riot Games", r"D:\Riot Games"],
    "steam":             [r"C:\Program Files (x86)\Steam",
                          r"D:\Steam", r"D:\SteamLibrary"],
    "epic games":        [r"C:\Program Files\Epic Games",
                          r"D:\Epic Games"],
    "epic":              [r"C:\Program Files\Epic Games",
                          r"D:\Epic Games"],
}


def _dir_size_bytes(path: str, _budget_files: int = 400_000) -> int:
    total = 0
    seen = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                continue
            seen += 1
            if seen > _budget_files:   # safety cap on pathological trees
                return total
    return total


def _human(nbytes: int) -> str:
    if nbytes >= 1024 ** 3:
        return f"{nbytes / 1024**3:.1f} GB"
    if nbytes >= 1024 ** 2:
        return f"{nbytes / 1024**2:.0f} MB"
    return f"{nbytes / 1024:.0f} KB"


def get_game_size(name: str) -> dict:
    """Estimate how much disk a game/app install uses."""
    key = (name or "").lower().strip()
    if not key:
        return {"error": "no game given"}

    # 1. Known direct folders
    for alias, paths in _KNOWN_GAME_DIRS.items():
        if alias in key or key in alias:
            for p in paths:
                if os.path.isdir(p):
                    size = _dir_size_bytes(p)
                    return {"name": name.title(), "path": p,
                            "bytes": size, "size": _human(size)}

    # 2. Search library roots for a folder fuzzy-matching the name
    import difflib
    best = None
    for root in _GAME_LIBRARY_ROOTS:
        if not os.path.isdir(root):
            continue
        try:
            for entry in os.listdir(root):
                full = os.path.join(root, entry)
                if not os.path.isdir(full):
                    continue
                ratio = difflib.SequenceMatcher(
                    None, key, entry.lower()).ratio()
                if key in entry.lower() or entry.lower() in key:
                    ratio = max(ratio, 0.95)
                if ratio > 0.6 and (best is None or ratio > best[0]):
                    best = (ratio, entry, full)
        except OSError:
            continue
    if best:
        size = _dir_size_bytes(best[2])
        return {"name": best[1], "path": best[2],
                "bytes": size, "size": _human(size)}
    return {"error": f"I couldn't find an install folder for '{name}'."}


def get_largest_folders(base: str = "", top: int = 5) -> dict:
    """
    Find the largest immediate sub-folders under `base`.
    Default base = the user folder. Returns sorted list.
    """
    if not base:
        base = config.BASE_USER_FOLDER
    base = _resolve_folder(base) or base
    if not os.path.isdir(base):
        return {"error": f"Folder not found: {base}"}
    sizes = []
    try:
        for entry in os.listdir(base):
            full = os.path.join(base, entry)
            if os.path.isdir(full):
                try:
                    sizes.append((_dir_size_bytes(full), entry))
                except Exception:
                    continue
    except Exception as e:
        return {"error": str(e)}
    sizes.sort(reverse=True)
    top_list = [{"name": n, "bytes": b, "size": _human(b)}
                for b, n in sizes[:top]]
    return {"base": base, "folders": top_list}


# ── Disk ──────────────────────────────────────────────────────────────────────

def get_disk_space(drive: str = "C") -> dict:
    letter = drive.upper().rstrip(":\\").strip() or "C"
    path   = f"{letter}:\\"
    try:
        total, used, free = shutil.disk_usage(path)
        def gb(b): return round(b / (1024 ** 3), 1)
        return {
            "drive":    letter,
            "free_gb":  gb(free),
            "used_gb":  gb(used),
            "total_gb": gb(total),
            "pct_free": round(free / total * 100),
        }
    except Exception as e:
        return {"error": str(e)}


# ── App control ───────────────────────────────────────────────────────────────

def open_app(app_name: str) -> dict:
    result = actions.open_app(app_name)
    return {"result": result, "app": app_name}


def close_app(app_name: str) -> dict:
    result = actions.close_app(app_name)
    return {"result": result, "app": app_name}


def sleep_pc() -> dict:
    result = actions.sleep_pc()
    return {"result": result}


# ── Web ───────────────────────────────────────────────────────────────────────

def open_website(url: str) -> dict:
    result = actions.open_url(url)
    return {"result": result, "url": url}


def open_named_site(site: str) -> dict:
    result = actions.open_named_site(site)
    return {"result": result, "site": site}


def search_google(query: str) -> dict:
    result = actions.search_google(query)
    return {"result": result, "query": query}


def search_youtube(query: str) -> dict:
    result = actions.search_youtube(query)
    return {"result": result, "query": query}


def search_web(query: str) -> dict:
    """
    Safe real-time web search — opens a live DuckDuckGo search in the browser.
    No API key needed. No Ollama inventing facts.
    """
    import urllib.parse
    url = "https://duckduckgo.com/?q=" + urllib.parse.quote_plus(query)
    try:
        webbrowser.open(url)
        return {"result": f"Opening a live search for: {query}", "query": query, "url": url}
    except Exception as e:
        return {"error": str(e)}


# ── Folders ───────────────────────────────────────────────────────────────────

def open_folder(folder: str) -> dict:
    folder_l = folder.lower()
    if "download" in folder_l:
        return {"result": actions.open_downloads()}
    if "document" in folder_l:
        return {"result": actions.open_documents()}
    if "maki" in folder_l:
        return {"result": actions.open_maki_folder()}
    if "n8n" in folder_l:
        return {"result": actions.open_n8n_projects()}
    return {"result": actions.open_folder(folder)}


# ── Meta / self-knowledge ─────────────────────────────────────────────────────

def get_current_mode_and_model() -> dict:
    """Return what mode Maki is running in and which model."""
    from brain import get_mode, MODE_OLLAMA
    mode   = get_mode()
    ollama = config.OLLAMA_MODEL if mode == MODE_OLLAMA else None
    return {
        "mode":        mode,
        "ollama_model": ollama,
        "description": (
            f"Ollama mode using {config.OLLAMA_MODEL}"
            if mode == MODE_OLLAMA
            else "Basic mode — fast keyword classifier, no AI model"
        ),
    }


def get_permissions() -> dict:
    return {
        "can_do": [
            "Open or close any installed app (Chrome, Discord, Spotify, VS Code, League, Valorant, etc.)",
            "Search Google or YouTube",
            "Open any website or URL",
            "Open folders (Downloads, Documents, etc.)",
            "Tell the time anywhere in the world",
            "Tell the date or calculate what day a date falls on",
            "Check disk space on any drive",
            "Put the PC to sleep",
            "Have a natural conversation and answer general questions",
        ],
        "will_not_do": [
            "Delete or permanently modify files",
            "Send emails, messages, or DMs",
            "Make purchases or submit forms",
            "Change passwords or account settings",
            "Install or uninstall software",
            "Execute anything without clear intent confirmed by you",
        ],
    }


# ── Process checking ──────────────────────────────────────────────────────────

_PROC_MAP = {
    # Browsers
    "chrome":            "chrome.exe",
    "google chrome":     "chrome.exe",
    "browser":           "chrome.exe",
    "edge":              "msedge.exe",
    "microsoft edge":    "msedge.exe",
    "firefox":           "firefox.exe",
    # Communication
    "discord":           "Discord.exe",
    "spotify":           "Spotify.exe",
    # Riot
    "riot":              "RiotClientServices.exe",
    "riot client":       "RiotClientServices.exe",
    "riot games":        "RiotClientServices.exe",
    "league":            "LeagueClient.exe",
    "league of legends": "LeagueClient.exe",
    "lol":               "LeagueClient.exe",
    "valorant":          "VALORANT.exe",
    # Games
    "steam":             "steam.exe",
    "rocket league":     "RocketLeague.exe",
    "epic games":        "EpicGamesLauncher.exe",
    # Dev
    "docker":            "Docker Desktop.exe",
    "docker desktop":    "Docker Desktop.exe",
    "vs code":           "Code.exe",
    "vscode":            "Code.exe",
    "code":              "Code.exe",
    # System
    "notepad":           "notepad.exe",
    "calculator":        "Calculator.exe",
    "task manager":      "Taskmgr.exe",
    "nvidia":            "nvcplui.exe",
    "geforce experience":"NVIDIA GeForce Experience.exe",
    "word":              "WINWORD.EXE",
    "excel":             "EXCEL.EXE",
    "powerpoint":        "POWERPNT.EXE",
}


def check_process(name: str) -> dict:
    """Check if a named process is currently running."""
    try:
        import psutil
    except ImportError:
        return {"error": "psutil not installed — pip install psutil"}

    clean = name.lower().strip()
    proc_name = _PROC_MAP.get(clean)

    # Fuzzy fallback: find closest alias
    if not proc_name:
        for alias, exe in _PROC_MAP.items():
            if alias in clean or clean in alias:
                proc_name = exe
                break

    if not proc_name:
        return {"error": f"Unknown app: '{name}'", "name": name, "running": False}

    proc_name_l = proc_name.lower()
    try:
        running = any(
            p.info["name"].lower() == proc_name_l
            for p in psutil.process_iter(["name"])
            if p.info["name"]
        )
    except Exception as e:
        return {"error": str(e), "name": name, "running": False}

    return {"name": name, "process": proc_name, "running": running}


# ── List common running apps ──────────────────────────────────────────────────

_COMMON_PROCS = {
    "chrome.exe":                  "Chrome",
    "discord.exe":                 "Discord",
    "spotify.exe":                 "Spotify",
    "steam.exe":                   "Steam",
    "riotclientservices.exe":      "Riot Client",
    "leagueclient.exe":            "League of Legends",
    "valorant.exe":                "VALORANT",
    "rocketleague.exe":            "Rocket League",
    "epicgameslauncher.exe":       "Epic Games",
    "code.exe":                    "VS Code",
    "docker desktop.exe":          "Docker Desktop",
    "firefox.exe":                 "Firefox",
    "msedge.exe":                  "Edge",
    "notepad.exe":                 "Notepad",
    "taskmgr.exe":                 "Task Manager",
    "winword.exe":                 "Word",
    "excel.exe":                   "Excel",
    "powerpnt.exe":                "PowerPoint",
    "python.exe":                  "Python",
    "pythonw.exe":                 "Python (background)",
    "obs64.exe":                   "OBS",
    "slack.exe":                   "Slack",
    "teams.exe":                   "Teams",
    "zoom.exe":                    "Zoom",
    "nvidia geforce experience.exe": "GeForce Experience",
}


def list_running_common_apps() -> dict:
    """Return list of well-known apps currently running (uses psutil)."""
    try:
        import psutil
    except ImportError:
        return {"error": "psutil not installed", "running": []}
    found = []
    try:
        for p in psutil.process_iter(["name"]):
            raw = (p.info.get("name") or "").lower()
            if raw in _COMMON_PROCS:
                label = _COMMON_PROCS[raw]
                if label not in found:
                    found.append(label)
    except Exception:
        pass
    return {"running": sorted(found)}


# ── Tool registry — maps tool names → callables ───────────────────────────────

REGISTRY = {
    "get_current_time":        get_current_time,
    "get_current_date":        get_current_date,
    "get_time_in":             get_time_in,
    "add_time_offset":         add_time_offset,
    "calculate_day_of_date":   calculate_day_of_date,
    "get_disk_space":          get_disk_space,
    "get_folder_size":         get_folder_size,
    "get_game_size":           get_game_size,
    "get_largest_folders":     get_largest_folders,
    "open_app":                open_app,
    "close_app":               close_app,
    "sleep_pc":                sleep_pc,
    "open_website":            open_website,
    "open_named_site":         open_named_site,
    "search_google":           search_google,
    "search_youtube":          search_youtube,
    "search_web":              search_web,
    "open_folder":             open_folder,
    "get_current_mode_and_model": get_current_mode_and_model,
    "get_permissions":         get_permissions,
    "check_process":           check_process,
    "list_running_common_apps": list_running_common_apps,
}


def call(name: str, **kwargs):
    """Call a tool by name. Returns tool result dict or error dict."""
    fn = REGISTRY.get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}"}
    try:
        return fn(**kwargs)
    except Exception as e:
        logger.error("Tool %s failed: %s", name, e)
        return {"error": str(e)}
