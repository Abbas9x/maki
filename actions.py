"""
actions.py - Everything Maki is allowed to do on your PC.
"""

import glob as _glob, os, shutil, subprocess, webbrowser, datetime, logging
from zoneinfo import ZoneInfo
import config

logger = logging.getLogger(__name__)

# ── Timezone map ──────────────────────────────────────────────────────────────
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
}

# Windows URI schemes — open built-in apps without knowing exe paths
_WIN_URIS = {
    "photos":     "ms-photos:",
    "settings":   "ms-settings:",
    "store":      "ms-windows-store:",
    "calendar":   "outlookcal:",
    "maps":       "bingmaps:",
    "weather":    "msnweather:",
    "camera":     "microsoft.windows.camera:",
    "clock":      "ms-clock:",
    "paint":      "ms-paint:",
    "snipping":   "ms-screensketch:",
    "snip":       "ms-screensketch:",
    "whiteboard": "ms-whiteboard:",
}

# Process names for taskkill
_PROC_NAMES = {
    "discord":           "Discord.exe",
    "chrome":            "chrome.exe",
    "google chrome":     "chrome.exe",
    "browser":           "chrome.exe",
    "spotify":           "Spotify.exe",
    "docker":            "Docker Desktop.exe",
    "league of legends": "LeagueClient.exe",
    "league":            "LeagueClient.exe",
    "valorant":          "VALORANT.exe",
    "riot client":       "RiotClientServices.exe",
    "riot games":        "RiotClientServices.exe",
    "riot":              "RiotClientServices.exe",
    "rocket league":     "RocketLeague.exe",
    "rocket":            "RocketLeague.exe",
    "steam":             "steam.exe",
    "vs code":           "Code.exe",
    "vscode":            "Code.exe",
    "visual studio code":"Code.exe",
    "notepad":           "notepad.exe",
    "powershell":        "powershell.exe",
    "windows powershell":"powershell.exe",
    "cmd":               "cmd.exe",
    "command prompt":    "cmd.exe",
    "terminal":          "WindowsTerminal.exe",
    "windows terminal":  "WindowsTerminal.exe",
    "calculator":        "Calculator.exe",
    "epic games":        "EpicGamesLauncher.exe",
    "epic":              "EpicGamesLauncher.exe",
    "task manager":      "Taskmgr.exe",
    "photos":            "Microsoft.Photos.exe",
    "nvidia":            "nvcplui.exe",
    "nvidia control panel": "nvcplui.exe",
    "geforce experience":"NVIDIA GeForce Experience.exe",
    "edge":              "msedge.exe",
    "microsoft edge":    "msedge.exe",
    "firefox":           "firefox.exe",
    "word":              "WINWORD.EXE",
    "excel":             "EXCEL.EXE",
    "powerpoint":        "POWERPNT.EXE",
}

# Riot Client args for each Riot game
_RIOT_PRODUCTS = {
    "league of legends": "league_of_legends",
    "league":            "league_of_legends",
    "valorant":          "valorant",
}
_RIOT_CLIENT_PATHS = [
    r"C:\Riot Games\Riot Client\RiotClientServices.exe",
    r"D:\Riot Games\Riot Client\RiotClientServices.exe",
    r"C:\Riot Games\Riot Client\RiotClient.exe",
    r"D:\Riot Games\Riot Client\RiotClient.exe",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_exe(paths: list) -> str | None:
    for p in paths:
        ms = _glob.glob(p)
        if ms:
            return ms[0]
        if os.path.exists(p):
            return p
    return None


def _find_riot_client() -> str | None:
    return _find_exe(_RIOT_CLIENT_PATHS)


def _find_start_menu(name: str) -> str | None:
    """Search Windows Start Menu .lnk shortcuts by approximate name."""
    dirs = [
        r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs",
        os.path.join(os.environ.get("APPDATA",""), r"Microsoft\Windows\Start Menu\Programs"),
    ]
    name_l = name.lower()
    best = None
    for d in dirs:
        for lnk in _glob.glob(os.path.join(d, "**", "*.lnk"), recursive=True):
            base = os.path.splitext(os.path.basename(lnk))[0].lower()
            if base == name_l:
                return lnk
            if name_l in base and best is None:
                best = lnk
    return best


def _open_app(name: str) -> str:
    clean = name.lower().replace("_", " ").replace("-", " ").strip()
    if not clean:
        return "Which app would you like me to open?"

    # 0. Discord — special multi-strategy launch to avoid Windows error popup
    if clean == "discord":
        # 0a. config override path
        cfg_path = getattr(config, "DISCORD_PATH", "")
        if cfg_path and os.path.exists(cfg_path):
            try:
                subprocess.Popen([cfg_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return "Opening Discord."
            except Exception as exc:
                logger.error("Discord config path failed: %s", exc)

        local = os.environ.get("LOCALAPPDATA", "")

        # 0b. Update.exe --processStart (standard Discord install)
        update_exe = os.path.join(local, "Discord", "Update.exe")
        if os.path.exists(update_exe):
            try:
                subprocess.Popen(
                    [update_exe, "--processStart", "Discord.exe"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                return "Opening Discord."
            except Exception as exc:
                logger.error("Discord Update.exe failed: %s", exc)

        # 0c. Glob latest app-* folder
        discord_glob = os.path.join(local, "Discord", "app-*", "Discord.exe")
        matches = sorted(_glob.glob(discord_glob))
        if matches:
            try:
                subprocess.Popen(
                    [matches[-1]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                return "Opening Discord."
            except Exception as exc:
                logger.error("Discord app-* launch failed: %s", exc)

        # 0d. Windows Start Menu shortcut
        lnk = _find_start_menu("discord")
        if lnk:
            try:
                os.startfile(lnk)
                return "Opening Discord."
            except Exception as exc:
                logger.error("Discord LNK failed: %s", exc)

        # 0e. Shell start as absolute last resort (silent)
        try:
            subprocess.Popen(
                'start "" "discord:"', shell=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return "Opening Discord."
        except Exception:
            pass

        return ("I couldn't find Discord on this machine. "
                "Set DISCORD_PATH in config.py or reinstall Discord.")

    # 1. Riot Games — always launch via Riot Client with proper product flag
    if clean in _RIOT_PRODUCTS:
        product = _RIOT_PRODUCTS[clean]
        riot_exe = _find_riot_client()
        if riot_exe:
            try:
                cmd = f'"{riot_exe}" --launch-product={product} --launch-patchline=live'
                subprocess.Popen(cmd, shell=True)
                return f"Opening {clean.title()}."
            except Exception as exc:
                logger.error("Riot Client launch failed: %s", exc)
        # Fallback: try direct exe paths if Riot Client not found
        for key in [clean, name.lower()]:
            paths = config.APP_PATHS.get(key, [])
            exe = _find_exe(paths)
            if exe:
                try:
                    subprocess.Popen(f'"{exe}"', shell=True)
                    return f"Opening {clean.title()}."
                except Exception as exc:
                    logger.error("Direct launch failed %s: %s", exe, exc)
        return f"Couldn't find the Riot Client. Make sure Riot Games is installed."

    # 2. Windows URI scheme (Photos, Settings, Store, etc.)
    uri = _WIN_URIS.get(clean)
    if uri:
        try:
            subprocess.Popen(f'start "" "{uri}"', shell=True)
            return f"Opening {clean.title()}."
        except Exception as exc:
            logger.error("URI launch failed %s: %s", uri, exc)

    # 3. Known path from config
    for key in [clean, name.lower()]:
        paths = config.APP_PATHS.get(key)
        if paths:
            exe = _find_exe(paths)
            if exe:
                try:
                    subprocess.Popen(f'"{exe}"', shell=True)
                    return f"Opening {clean.title()}."
                except Exception as exc:
                    logger.error("Launch failed %s: %s", exe, exc)
                try:
                    os.startfile(exe)
                    return f"Opening {clean.title()}."
                except Exception as exc:
                    logger.error("startfile failed %s: %s", exe, exc)
            return (f"I know {clean.title()} but its file wasn't found. "
                    "Make sure it's installed or update config.py.")

    # 4. Search Start Menu shortcuts (.lnk files)
    lnk = _find_start_menu(clean)
    if lnk:
        try:
            os.startfile(lnk)
            return f"Opening {clean.title()}."
        except Exception as exc:
            logger.error("LNK open failed: %s", exc)

    # 5. Windows shell 'start' — works for anything in PATH or Start Menu
    try:
        subprocess.Popen(
            f'start "" "{clean}"', shell=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return f"Trying to open {clean.title()}."
    except Exception as exc:
        logger.error("Shell start failed for %s: %s", clean, exc)

    return f"I couldn't find {clean.title()}. Add its path in config.py."


# ── Close apps ────────────────────────────────────────────────────────────────

def close_app(name: str) -> str:
    clean = name.lower().strip()
    proc  = _PROC_NAMES.get(clean)

    if not proc:
        for key, p in _PROC_NAMES.items():
            if clean in key or key in clean:
                proc = p
                break

    if proc:
        result = subprocess.run(
            f'taskkill /F /IM "{proc}"',
            shell=True, capture_output=True, text=True
        )
        return f"Closed {name.title()}." if result.returncode == 0 else f"{name.title()} isn't running."

    subprocess.run(f'taskkill /F /FI "WINDOWTITLE eq {clean}*"',
                   shell=True, capture_output=True)
    return f"Sent close signal to {name.title()}."


# ── System actions ────────────────────────────────────────────────────────────

def sleep_pc() -> str:
    """Put the computer to sleep using Windows power management."""
    try:
        subprocess.Popen(
            "rundll32.exe powrprof.dll,SetSuspendState 0,1,0",
            shell=True
        )
        return "Putting the PC to sleep now."
    except Exception as e:
        logger.error("Sleep failed: %s", e)
        return "Couldn't put the PC to sleep."


# ── Time & date ───────────────────────────────────────────────────────────────

def tell_time() -> str:
    now  = datetime.datetime.now()
    h    = now.strftime("%I").lstrip("0") or "12"
    m    = now.strftime("%M")
    ampm = now.strftime("%p")
    return f"It's {h}:{m} {ampm}." if m != "00" else f"It's {h} {ampm}."


def tell_time_in(location: str) -> str:
    loc     = location.lower().strip()
    tz_name = _TZ.get(loc)
    if not tz_name:
        for key, tz in _TZ.items():
            if key in loc or loc in key:
                tz_name = tz
                break
    if not tz_name:
        return (f"I don't know the timezone for {location.title()}. "
                "I know Pakistan, Dubai, London, New York, and more.")
    try:
        now  = datetime.datetime.now(ZoneInfo(tz_name))
        h    = now.strftime("%I").lstrip("0") or "12"
        m    = now.strftime("%M")
        ampm = now.strftime("%p")
        t    = f"{h}:{m} {ampm}" if m != "00" else f"{h} {ampm}"
        return f"It's {t} in {location.title()} right now."
    except Exception as exc:
        logger.error("Timezone error %s: %s", location, exc)
        return f"Couldn't get the time for {location.title()}."


def tell_date() -> str:
    now    = datetime.datetime.now()
    day    = now.day
    suffix = "th" if 11 <= day <= 13 else {1:"st",2:"nd",3:"rd"}.get(day % 10, "th")
    return f"Today is {now.strftime('%A, %B')} {day}{suffix}, {now.year}."


# ── Disk / system ─────────────────────────────────────────────────────────────

def check_disk_space(drive: str = "C") -> str:
    letter = drive.upper().rstrip(":\\").strip() or "C"
    path   = f"{letter}:\\"
    try:
        total, used, free = shutil.disk_usage(path)
        def gb(b): return b / (1024 ** 3)
        pct = free / total * 100
        return (f"Your {letter} drive has {gb(free):.1f} GB free out of {gb(total):.1f} GB total. "
                f"{gb(used):.1f} GB used — {pct:.0f}% free.")
    except Exception as e:
        return f"Couldn't read the {letter} drive: {e}"


# ── Web ───────────────────────────────────────────────────────────────────────

def open_url(url: str) -> str:
    if not url or url.strip() in ("", "."):
        return "Which website would you like me to open?"
    if not url.startswith("http"):
        url = "https://" + url
    # Friendly display name (strip https://www.)
    display = url.replace("https://","").replace("http://","").replace("www.","").split("/")[0]
    try:
        webbrowser.open(url)
        return f"Opening {display}."
    except Exception as e:
        logger.error("Browser error: %s", e)
        return "Couldn't open the browser."


def open_named_site(site: str) -> str:
    if not site:
        return "Which website would you like me to open?"
    url = config.URLS.get(site.lower())
    if url:
        return open_url(url)
    # Try as a domain
    return open_url(site)


def search_google(query: str) -> str:
    if not query:
        return "What should I search for on Google?"
    webbrowser.open(f"https://www.google.com/search?q={query.replace(' ','+')}")
    return f"Searching Google for: {query}"


def search_youtube(query: str) -> str:
    if not query:
        return "What should I search for on YouTube?"
    webbrowser.open(f"https://www.youtube.com/results?search_query={query.replace(' ','+')}")
    return f"Searching YouTube for: {query}"


# ── Apps ──────────────────────────────────────────────────────────────────────

def open_app(name: str)  -> str: return _open_app(name)
def open_discord()        -> str: return _open_app("discord")
def open_chrome()         -> str: return _open_app("chrome")
def open_docker()         -> str: return _open_app("docker")
def open_spotify()        -> str:
    result = _open_app("spotify")
    if "couldn't find" in result or "wasn't found" in result:
        try:
            webbrowser.open("spotify:")
            return "Opening Spotify."
        except Exception:
            pass
    return result

def open_n8n() -> str: return open_url(config.URLS["n8n"])


# ── Folders ───────────────────────────────────────────────────────────────────

def open_folder(path: str) -> str:
    if os.path.isdir(path):
        try:
            subprocess.Popen(["explorer", path])
            return f"Opening {os.path.basename(path)}."
        except Exception as e:
            return f"Couldn't open the folder: {e}"
    return f"Folder not found: {path}"


def open_downloads()    -> str: return open_folder(config.DOWNLOADS_FOLDER)
def open_documents()    -> str: return open_folder(config.DOCUMENTS_FOLDER)
def open_maki_folder()  -> str: return open_folder(config.MAKI_FOLDER)
def open_n8n_projects() -> str: return open_folder(config.N8N_PROJECTS_FOLDER)


# ── Info ──────────────────────────────────────────────────────────────────────

def capabilities() -> str:
    return (
        "I can open or close any app — Spotify, Discord, Chrome, League of Legends, "
        "Valorant, Steam, Docker, VS Code, and more. "
        "I can search Google or YouTube, open any website, "
        "check your disk space, put your PC to sleep, "
        "tell you the time anywhere in the world, open folders, and have a real conversation."
    )
