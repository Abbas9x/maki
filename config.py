import os
from dotenv import load_dotenv
load_dotenv()

USER_NAME      = os.getenv("USER_NAME", "friend")   # set USER_NAME in .env to personalize
ASSISTANT_NAME = "Maki"
WAKE_PHRASE       = "hey maki"
WAKE_WORD_STRICT  = False  # Process all speech — wake word strips prefix but does NOT block

# ── Gemini (cloud AI — primary brain) ─────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL   = os.getenv("GEMINI_MODEL",   "gemini-2.5-flash")

# ── Provider priority ─────────────────────────────────────────────────────────
_prio_raw            = os.getenv("AI_PROVIDER_PRIORITY", "gemini,ollama,basic")
AI_PROVIDER_PRIORITY = [p.strip().lower() for p in _prio_raw.split(",")]

WAKE_ALTERNATIVES = [
    "hey maki","hey makey","hey macky","hey marky","hey marquee",
    "hey machi","hey hockey","hey make","hey markee","hey mocky",
    "okay maki","ok maki","hey mickey","hey mikey","hey mekey",
    "hey monkey","hey monty","hey margie","hey markey",
]
WAKE_SINGLE_WORDS = ["maki","makey","macky","machi"]

# ── Ollama ────────────────────────────────────────────────────────────────────
OLLAMA_URL     = os.getenv("OLLAMA_URL",   "http://localhost:11434/api/chat")
OLLAMA_MODEL   = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_TIMEOUT = 6   # V3: tighter — if Ollama is slow, fall back fast

# ── TTS ───────────────────────────────────────────────────────────────────────
# edge-tts (AriaNeural) is primary — pyttsx3 is fallback only
TTS_RATE          = 170   # V3: slightly slower = sounds more natural
TTS_VOLUME        = 1.0
TTS_PREFER_FEMALE = True
TTS_VOICE_INDEX   = 0
VOICE_PREFERENCE  = "female"   # "female" | "male" | "default"
EDGE_VOICE        = "en-US-AriaNeural"   # Microsoft neural voice (free, no API key)
EDGE_RATE         = "+5%"                # slightly faster than neutral
EDGE_VOLUME       = "+0%"

# ── V9.2 / V10 Speech-to-text ─────────────────────────────────────────────────
# faster-whisper = local, accurate, offline (fixes mishears at the source).
# google         = the old free web recognizer (fallback if whisper unavailable).
STT_ENGINE     = "faster-whisper"
WHISPER_MODEL  = "small.en"   # tiny.en | base.en | small.en  (bigger = more accurate)
WHISPER_DEVICE = "cuda"       # "cuda" (RTX GPU, fast+accurate) — auto-falls back to "cpu"

# ── V10 Agentic brain ─────────────────────────────────────────────────────────
USE_AGENTIC_BRAIN     = True   # LLM decides & calls tools (vs. old regex-only routing)
OLLAMA_KEEP_ALIVE     = "30m"  # keep qwen3 warm in VRAM so it never cold-starts
OLLAMA_AGENT_TIMEOUT  = 35     # V14: bumped 14→35; vision contention + tool rounds need more

# ── Cerebras (V15.2: super-fast text brain, free 1M tok/day) ─────────────────
CEREBRAS_API_KEY  = os.getenv("CEREBRAS_API_KEY", "").strip()
CEREBRAS_MODEL    = os.getenv("CEREBRAS_MODEL", "gpt-oss-120b")  # see notes below
CEREBRAS_URL      = "https://api.cerebras.ai/v1/chat/completions"
CEREBRAS_TIMEOUT  = 10
# Model choices on the free tier:
#   gpt-oss-120b                       — OpenAI 120B; best for instructions + tools (~600ms)
#   qwen-3-235b-a22b-instruct-2507     — 235B MoE Qwen; biggest + smartest (~550ms)
#   llama3.1-8b                        — 8B; fastest but dumbest (~500ms)
# Vision: Cerebras has NO vision; we keep Gemini + local qwen3-vl for that.

# ── Mic ───────────────────────────────────────────────────────────────────────
MIC_PAUSE_THRESHOLD   = 0.65  # V3: shorter silence → processes faster
MIC_PHRASE_TIME_LIMIT = 12    # V3: was 15 — shorter cap reduces max wait
MIC_DYNAMIC_ENERGY    = True
MIC_ENERGY_THRESHOLD  = 80

# ── V3 Speed / behaviour flags ────────────────────────────────────────────────
USE_FAST_COMMAND_PATH = True   # skip Ollama for clear commands (open/close/search/time)
FAST_COMMAND_TIMEOUT  = 0.0    # seconds to wait before fast path fires (0 = instant)

# ── V6 Smart Router + AI Timeouts ─────────────────────────────────────────────
GEMINI_TIMEOUT_SECONDS         = 5    # short — fall to Ollama fast
OLLAMA_TIMEOUT_SECONDS         = 5    # short — keep voice loop snappy

USE_SMART_ROUTER               = True # route casual chat to Ollama, complex to Gemini
PREFER_OLLAMA_FOR_CASUAL_CHAT  = True # short emotional/social → Ollama (saves Gemini quota)
USE_GEMINI_FOR_COMPLEX_REASONING = True
FAST_PATH_FIRST                = True # Python tools always tried before any AI call

# ── Discord (V5) ──────────────────────────────────────────────────────────────
DISCORD_PATH = ""              # leave empty for auto-detect; or full path to Discord.exe

# ── App aliases (normalised → canonical name used in actions.py) ──────────────
APP_ALIASES = {
    "riot games":       "riot client",
    "riot client":      "riot client",
    "riot":             "riot client",
    "league":           "league of legends",
    "lol":              "league of legends",
    "google chrome":    "chrome",
    "internet":         "chrome",
    "browser":          "chrome",
    "code":             "vs code",
    "visual studio":    "vs code",
    "vscode":           "vs code",
    "docker desktop":   "docker",
    "calculator":       "calculator",
    "calc":             "calculator",
    "task manager":     "task manager",
    "taskmgr":          "task manager",
    "geforce":          "geforce experience",
    "snipping tool":    "snipping",
    "snip":             "snipping",
}

# ── Folders ───────────────────────────────────────────────────────────────────
BASE_USER_FOLDER    = os.path.expanduser("~")   # current user's home dir — portable
DOWNLOADS_FOLDER    = os.path.join(BASE_USER_FOLDER, "Downloads")
DOCUMENTS_FOLDER    = os.path.join(BASE_USER_FOLDER, "Documents")
MAKI_FOLDER         = os.path.join(BASE_USER_FOLDER, "projectmaki")
N8N_PROJECTS_FOLDER = os.path.join(BASE_USER_FOLDER, "n8n-projects")

# ── Websites ──────────────────────────────────────────────────────────────────
URLS = {
    "gmail":    "https://mail.google.com",
    "youtube":  "https://www.youtube.com",
    "google":   "https://www.google.com",
    "n8n":      "http://localhost:5678",
    "github":   "https://github.com",
    "chatgpt":  "https://chat.openai.com",
    "claude":   "https://claude.ai",
    "reddit":   "https://www.reddit.com",
    "twitter":  "https://twitter.com",
    "x":        "https://twitter.com",
}

# ── App Paths ─────────────────────────────────────────────────────────────────
_U = BASE_USER_FOLDER
APP_PATHS = {
    "discord": [
        os.path.join(_U, r"AppData\Local\Discord\Update.exe"),
        os.path.join(_U, r"AppData\Local\Discord\app-1.0.9030\Discord.exe"),
        r"C:\Program Files\Discord\Discord.exe",
    ],
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(_U, r"AppData\Local\Google\Chrome\Application\chrome.exe"),
    ],
    "browser": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(_U, r"AppData\Local\Google\Chrome\Application\chrome.exe"),
    ],
    "docker": [
        r"C:\Program Files\Docker\Docker\Docker Desktop.exe",
    ],
    "spotify": [
        os.path.join(_U, r"AppData\Roaming\Spotify\Spotify.exe"),
        os.path.join(_U, r"AppData\Local\Microsoft\WindowsApps\Spotify.exe"),
    ],
    "riot client": [
        r"C:\Riot Games\Riot Client\RiotClientServices.exe",
        r"D:\Riot Games\Riot Client\RiotClientServices.exe",
    ],
    "riot games": [
        r"C:\Riot Games\Riot Client\RiotClientServices.exe",
        r"D:\Riot Games\Riot Client\RiotClientServices.exe",
    ],
    "league of legends": [
        r"C:\Riot Games\League of Legends\LeagueClient.exe",
        r"D:\Riot Games\League of Legends\LeagueClient.exe",
        r"C:\Riot Games\Riot Client\RiotClientServices.exe",
        r"D:\Riot Games\Riot Client\RiotClientServices.exe",
    ],
    "league": [
        r"C:\Riot Games\League of Legends\LeagueClient.exe",
        r"D:\Riot Games\League of Legends\LeagueClient.exe",
        r"C:\Riot Games\Riot Client\RiotClientServices.exe",
        r"D:\Riot Games\Riot Client\RiotClientServices.exe",
    ],
    "valorant": [
        r"C:\Riot Games\VALORANT\live\VALORANT.exe",
        r"C:\Riot Games\Riot Client\RiotClientServices.exe",
        r"D:\Riot Games\Riot Client\RiotClientServices.exe",
    ],
    "rocket league": [
        r"C:\Program Files\Epic Games\rocketleague\Binaries\Win64\RocketLeague.exe",
        r"D:\Program Files\Epic Games\rocketleague\Binaries\Win64\RocketLeague.exe",
        r"C:\Epic Games\rocketleague\Binaries\Win64\RocketLeague.exe",
        r"D:\Epic Games\rocketleague\Binaries\Win64\RocketLeague.exe",
    ],
    "rocket": [
        r"C:\Program Files\Epic Games\rocketleague\Binaries\Win64\RocketLeague.exe",
        r"D:\Program Files\Epic Games\rocketleague\Binaries\Win64\RocketLeague.exe",
        r"C:\Epic Games\rocketleague\Binaries\Win64\RocketLeague.exe",
        r"D:\Epic Games\rocketleague\Binaries\Win64\RocketLeague.exe",
    ],
    "epic games": [
        os.path.join(_U, r"AppData\Local\EpicGamesLauncher\Portal\Binaries\Win64\EpicGamesLauncher.exe"),
        r"C:\Program Files (x86)\Epic Games\Launcher\Portal\Binaries\Win64\EpicGamesLauncher.exe",
    ],
    "epic": [
        os.path.join(_U, r"AppData\Local\EpicGamesLauncher\Portal\Binaries\Win64\EpicGamesLauncher.exe"),
        r"C:\Program Files (x86)\Epic Games\Launcher\Portal\Binaries\Win64\EpicGamesLauncher.exe",
    ],
    "steam": [
        r"C:\Program Files (x86)\Steam\steam.exe",
        r"C:\Program Files\Steam\steam.exe",
    ],
    "vs code": [
        os.path.join(_U, r"AppData\Local\Programs\Microsoft VS Code\Code.exe"),
        r"C:\Program Files\Microsoft VS Code\Code.exe",
    ],
    "vscode": [
        os.path.join(_U, r"AppData\Local\Programs\Microsoft VS Code\Code.exe"),
    ],
    "notepad":      [r"C:\Windows\System32\notepad.exe"],
    "explorer":     [r"C:\Windows\explorer.exe"],
    "powershell": [
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        r"C:\Program Files\PowerShell\7\pwsh.exe",
    ],
    "cmd":          [r"C:\Windows\System32\cmd.exe"],
    "windows terminal": [
        os.path.join(_U, r"AppData\Local\Microsoft\WindowsApps\wt.exe"),
        r"C:\Windows\System32\wt.exe",
    ],
    "calculator":   [r"C:\Windows\System32\calc.exe"],
    "task manager": [r"C:\Windows\System32\Taskmgr.exe"],
    "nvidia": [
        r"C:\Windows\System32\nvcplui.exe",
        r"C:\Program Files\NVIDIA Corporation\Control Panel Client\nvcplui.exe",
        r"C:\Program Files\NVIDIA Corporation\NVIDIA GeForce Experience\NVIDIA GeForce Experience.exe",
    ],
    "nvidia control panel": [
        r"C:\Windows\System32\nvcplui.exe",
        r"C:\Program Files\NVIDIA Corporation\Control Panel Client\nvcplui.exe",
    ],
    "geforce experience": [
        r"C:\Program Files\NVIDIA Corporation\NVIDIA GeForce Experience\NVIDIA GeForce Experience.exe",
    ],
    "edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "firefox": [
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    ],
    "word": [
        r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE",
        r"C:\Program Files (x86)\Microsoft Office\root\Office16\WINWORD.EXE",
    ],
    "excel": [
        r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE",
        r"C:\Program Files (x86)\Microsoft Office\root\Office16\EXCEL.EXE",
    ],
    "powerpoint": [
        r"C:\Program Files\Microsoft Office\root\Office16\POWERPNT.EXE",
        r"C:\Program Files (x86)\Microsoft Office\root\Office16\POWERPNT.EXE",
    ],
}

# ── Safety ────────────────────────────────────────────────────────────────────
RISKY_KEYWORDS = [
    # V14.6: bare 'delete'/'remove' was flagging edit commands (Backspace,
    # Ctrl+A then Delete). Require destructive phrasing only.
    "delete file","delete files","delete folder","delete the folder",
    "delete everything from","wipe my","wipe the disk","wipe drive",
    "format drive","format the disk","format c:","format d:",
    "uninstall",
    "send email","send message","send a message","send text","send a text",
    "send dm","send direct","message someone","text someone",
    "buy","purchase","pay","order",
    "submit","apply","enroll",
    "rename file","move file","overwrite file",
    "change password",
]
