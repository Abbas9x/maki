"""
agent.py — V10 agentic brain.

THE SHIFT: the LLM is the brain, not a fallback. It sees the user's message
plus Maki's tools, and *decides* — just talk, or call tools then talk. This is
what makes Maki think instead of pattern-match like a to-do bot.

  Gemini path : native automatic function calling (python-genai SDK loops for us)
  Ollama path : manual tool-call loop via /api/chat (qwen3:8b, kept warm in VRAM)
  Fallback    : graceful plain conversation — NEVER a "rate-limited" status dump

agent.py is self-contained for tool execution; it lazily imports `brain` only
for the shared Gemini client + cooldown state (avoids a circular import).
"""

from __future__ import annotations
import datetime as _dt
import json, logging, re, time
import requests

import config, memory

logger = logging.getLogger(__name__)

# Optional tool backends — degrade gracefully if a module is missing
try: import tools
except Exception: tools = None
try: import actions
except Exception: actions = None
try: import world_time_tools
except Exception: world_time_tools = None
try: import weather_tools
except Exception: weather_tools = None
try: import web_tools
except Exception: web_tools = None
try: import window_tools
except Exception: window_tools = None
try: import screenshot_tools
except Exception: screenshot_tools = None
try: import vision_tools           # V13: see the screen
except Exception: vision_tools = None
try: import screen_control         # V13: act on the screen
except Exception: screen_control = None
try: import ui_tree                # V13: UIAutomation accessibility tree
except Exception: ui_tree = None

_OLLAMA_CHAT_URL = getattr(config, "OLLAMA_URL", "http://localhost:11434/api/chat")
_KEEP_ALIVE      = getattr(config, "OLLAMA_KEEP_ALIVE", "30m")
_AGENT_TIMEOUT   = getattr(config, "OLLAMA_AGENT_TIMEOUT", 14)


# ══════════════════════════════════════════════════════════════════════════════
# TOOLS — thin wrappers over Maki's existing capabilities. Each returns a short
# string (the observation the model reasons over). Docstrings ARE the schema
# Gemini reads, so keep them clear.
# ══════════════════════════════════════════════════════════════════════════════

def get_current_time() -> str:
    """Get the current local time on this PC. Use for 'what time is it'."""
    return f"It's {tools.get_current_time()}." if tools else "Time tool unavailable."


def get_current_date() -> str:
    """Get today's date. Use for 'what's the date / what day is it'."""
    return f"Today is {tools.get_current_date()}." if tools else "Date tool unavailable."


def get_time_in(place: str) -> str:
    """Get the current time in a city or country (e.g. 'London', 'Tokyo', 'Pakistan')."""
    if world_time_tools:
        return world_time_tools.speak_time_in(place)
    return "World-time tool unavailable."


def get_weather(city: str) -> str:
    """Get current live weather for a city (temperature + conditions)."""
    if not weather_tools:
        return "Weather tool unavailable."
    r = weather_tools.get_weather(city)
    return r.get("summary") or r.get("error", "Couldn't get weather.")


def open_app(name: str) -> str:
    """Open / launch an application or game on the PC by name (e.g. 'discord', 'chrome', 'spotify')."""
    if not actions:
        return "App control unavailable."
    res = actions.open_app(name)
    return res if isinstance(res, str) else str(res)


def close_app(name: str) -> str:
    """Close / quit a running application by name.
    DO NOT call this unless the user EXPLICITLY said 'close', 'quit', 'exit',
    'kill', or 'shut down'. Saying 'open', 'go to', 'switch to', 'bring to
    front' must NEVER result in closing an app."""
    if not actions:
        return "App control unavailable."
    res = actions.close_app(name)
    return res if isinstance(res, str) else str(res)


def control_window(action: str, name: str) -> str:
    """Control a window. action is one of: minimize, maximize, restore, focus. name is the app/window."""
    if not window_tools:
        return "Window control unavailable."
    fn = {
        "minimize": window_tools.minimize_window,
        "maximize": window_tools.maximize_window,
        "restore":  window_tools.restore_window,
        "focus":    window_tools.focus_window,
    }.get(action.lower())
    if not fn:
        return f"Unknown window action '{action}'."
    r = fn(name)
    if "error" in r:
        return r["error"]
    return f"{action.title()}d {r.get('title', name)}."


def list_running_apps() -> str:
    """List the applications currently running / open on the PC."""
    if window_tools:
        running = window_tools.list_running_apps()
    elif tools:
        running = tools.list_running_common_apps().get("running", [])
    else:
        return "Process tool unavailable."
    return ("Running now: " + ", ".join(running)) if running else "No common apps detected running."


def take_screenshot(copy_to_clipboard: bool = False) -> str:
    """Take a screenshot of the screen. Set copy_to_clipboard=true to also copy the image."""
    if not screenshot_tools:
        return "Screenshot tool unavailable."
    r = (screenshot_tools.take_screenshot_to_clipboard() if copy_to_clipboard
         else screenshot_tools.take_screenshot(copy=False))
    if "error" in r:
        return r["error"]
    memory.set_last_screenshot(r["path"])
    return ("Screenshot saved and copied to clipboard." if r.get("copied")
            else "Screenshot saved.")


def web_search(query: str) -> str:
    """Look up live / current / factual info on the web and return rich context.
    Use for current events, facts you're unsure of, names, rosters, news,
    prices, definitions, etc. The result may contain MULTIPLE sources — read
    them and pick the one that actually answers the user's question."""
    if not web_tools:
        return "Web tool unavailable."
    hit = web_tools.live_lookup(query)
    # V11: prefer the full multi-source context so the model can reason over it
    ctx = hit.get("context")
    if ctx:
        return ctx[:2400]   # keep it bounded for the model's context window
    if hit.get("answer"):
        ans = hit["answer"]
        src = hit.get("source", "")
        return f"{ans}  (source: {src})" if src else ans
    return f"No solid web result for '{query}'. Tell the user honestly and offer to refine the query."


def get_folder_size(folder: str) -> str:
    """Get the on-disk size of a folder (e.g. 'projectmaki', 'downloads', 'screenshots')."""
    if not tools:
        return "Folder tool unavailable."
    r = tools.get_folder_size(folder)
    if "error" in r:
        return r["error"]
    return f"The {folder} folder is {r['size']} across {r['files']} files."


def get_game_size(game: str) -> str:
    """Get how much disk space a game / app install uses (e.g. 'league of legends', 'steam')."""
    if not tools:
        return "Game-size tool unavailable."
    r = tools.get_game_size(game)
    if "error" in r:
        return r["error"]
    return f"{r['name']} is using about {r['size']}."


def get_disk_space(drive: str = "C") -> str:
    """Get free/used disk space on a drive letter (default C)."""
    if not tools:
        return "Disk tool unavailable."
    r = tools.get_disk_space(drive)
    if "error" in r:
        return r["error"]
    return (f"{r['drive']} drive: {r['free_gb']} GB free of {r['total_gb']} GB "
            f"({r['pct_free']}% free).")


def recall_memory(topic: str) -> str:
    """Search the conversation history for something the user said earlier about `topic`."""
    hits = memory.search_history(topic, limit=4)
    if not hits:
        return f"Nothing in our history about '{topic}'."
    bits = []
    for h in hits[-3:]:
        who = "user" if h.get("role") == "user" else "you (Maki)"
        c = (h.get("content") or "").strip()
        if c:
            bits.append(f'{who} said "{c}"')
    return "From memory: " + "; ".join(bits) if bits else f"Found a vague reference to '{topic}'."


def search_youtube(query: str) -> str:
    """Open a YouTube search in the browser for the given query."""
    if not tools:
        return "YouTube tool unavailable."
    tools.search_youtube(query)
    return f"Searching YouTube for '{query}'."


def open_website(name_or_url: str) -> str:
    """Open a website by common name (youtube, gmail, github, reddit) or a URL."""
    if not tools:
        return "Web tool unavailable."
    try:
        tools.open_named_site(name_or_url)
    except Exception:
        tools.open_website(name_or_url)
    return f"Opening {name_or_url}."


def get_provider_status() -> str:
    """Report which AI models / engines Maki is currently using."""
    try:
        import brain
        return brain._mode_response()
    except Exception:
        return ("Gemini is the main brain, qwen3:8b runs locally as backup, "
                "and Python tools handle direct actions.")


# ── V13 Vision tools ────────────────────────────────────────────────────────
def look_at_screen(question: str = "") -> str:
    """Capture the screen and answer a question about it (vision LLM).
    Use whenever the user says 'look at my screen', 'what's on my screen',
    'read this', 'what does this say', 'see this', 'check my screen', etc.
    `question` is what to look for / answer about the screenshot."""
    if not vision_tools:
        return "Vision isn't available."
    q = question.strip() or "Describe what's on this screen briefly."
    return vision_tools.look_at_screen(q)


def describe_screen() -> str:
    """Free-form description of what's currently on the screen."""
    if not vision_tools:
        return "Vision isn't available."
    return vision_tools.describe_screen()


def read_text_on_screen() -> str:
    """Read the visible text on the screen (OCR via vision model)."""
    if not vision_tools:
        return "Vision isn't available."
    return vision_tools.read_text_on_screen()


# ── V13 Screen control tools ────────────────────────────────────────────────
def scroll(direction: str = "down", amount: int = 5) -> str:
    """Scroll the active window. direction = up|down|left|right. amount = wheel notches (1-20)."""
    if not screen_control: return "Screen control unavailable."
    return screen_control.scroll(direction, amount)


def type_text(text: str) -> str:
    """Type text into whatever has keyboard focus (input box, search bar, DM, etc)."""
    if not screen_control: return "Screen control unavailable."
    return screen_control.type_text(text)


def press_keys(combo: str) -> str:
    """Press a single key or chord. Examples: 'enter', 'esc', 'ctrl+t' (new tab),
    'ctrl+w' (close tab), 'alt+left' (browser back), 'win+d' (show desktop),
    'ctrl+shift+t' (reopen closed tab)."""
    if not screen_control: return "Screen control unavailable."
    return screen_control.press_keys(combo)


def click_at(x: int, y: int, double: bool = False) -> str:
    """Click the mouse at absolute screen pixel (x, y). Set double=true for double-click."""
    if not screen_control: return "Screen control unavailable."
    return screen_control.click_at(x, y, double=double)


def click_text(target_label: str) -> str:
    """Use the vision model to find a UI element by description (e.g.
    'the blue Submit button', 'the search bar at top', 'the Close X') and click it."""
    if not screen_control: return "Screen control unavailable."
    return screen_control.click_text(target_label)


def browser_action(action: str) -> str:
    """Perform a browser navigation action. action is one of:
    back | forward | refresh | new_tab | close_tab | switch_tab | reopen_tab"""
    if not screen_control: return "Screen control unavailable."
    fn = {
        "back":        screen_control.browser_back,
        "forward":     screen_control.browser_forward,
        "refresh":     screen_control.browser_refresh,
        "new_tab":     screen_control.new_tab,
        "close_tab":   screen_control.close_tab,
        "switch_tab":  screen_control.switch_tab,
        "reopen_tab":  screen_control.reopen_tab,
    }.get((action or "").lower().strip())
    if not fn:
        return f"Unknown browser action '{action}'."
    return fn()


def go_to_url(url: str) -> str:
    """Navigate the focused browser tab to a URL."""
    if not screen_control: return "Screen control unavailable."
    return screen_control.go_to_url(url)


def invoke_ui_element(name: str) -> str:
    """Find a UI element by name in the foreground window using Windows
    accessibility tree, and invoke (click) it. More reliable than coord-clicking
    for known apps (Chrome, VSCode, Discord, Office, File Explorer)."""
    if not ui_tree: return "UI tree reader unavailable."
    return ui_tree.invoke_element_by_name(name)


# Registry: name → python callable
_PY_TOOLS = [
    get_current_time, get_current_date, get_time_in, get_weather,
    open_app, close_app, control_window, list_running_apps, take_screenshot,
    web_search, get_folder_size, get_game_size, get_disk_space,
    recall_memory, search_youtube, open_website, get_provider_status,
    # V13 — vision + screen control
    look_at_screen, describe_screen, read_text_on_screen,
    scroll, type_text, press_keys, click_at, click_text,
    browser_action, go_to_url, invoke_ui_element,
]
_TOOL_MAP = {fn.__name__: fn for fn in _PY_TOOLS}


# ── JSON schemas for Ollama's /api/chat tools= param ─────────────────────────
def _schema(name, desc, props=None, required=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": props or {},
                "required": required or [],
            },
        },
    }

_S = lambda d: {"type": "string", "description": d}
_B = lambda d: {"type": "boolean", "description": d}

_OLLAMA_SCHEMAS = [
    _schema("get_current_time", "Current local time on this PC."),
    _schema("get_current_date", "Today's date."),
    _schema("get_time_in", "Current time in a city or country.",
            {"place": _S("city or country name")}, ["place"]),
    _schema("get_weather", "Current live weather for a city.",
            {"city": _S("city name")}, ["city"]),
    _schema("open_app", "Open/launch an app or game by name.",
            {"name": _S("app or game name")}, ["name"]),
    _schema("close_app", "Close/quit a running app by name.",
            {"name": _S("app name")}, ["name"]),
    _schema("control_window", "Minimize, maximize, restore or focus a window.",
            {"action": _S("minimize|maximize|restore|focus"),
             "name": _S("app/window name")}, ["action", "name"]),
    _schema("list_running_apps", "List apps currently running on the PC."),
    _schema("take_screenshot", "Take a screenshot of the screen.",
            {"copy_to_clipboard": _B("also copy the image to clipboard")}),
    _schema("web_search", "Look up live/current/factual info on the web.",
            {"query": _S("what to look up")}, ["query"]),
    _schema("get_folder_size", "On-disk size of a folder.",
            {"folder": _S("folder name e.g. projectmaki, downloads")}, ["folder"]),
    _schema("get_game_size", "Disk space a game/app install uses.",
            {"game": _S("game or app name")}, ["game"]),
    _schema("get_disk_space", "Free/used space on a drive.",
            {"drive": _S("drive letter, default C")}),
    _schema("recall_memory", "Search earlier conversation for a topic.",
            {"topic": _S("topic/keyword to recall")}, ["topic"]),
    _schema("search_youtube", "Open a YouTube search in the browser.",
            {"query": _S("search query")}, ["query"]),
    _schema("open_website", "Open a website by name or URL.",
            {"name_or_url": _S("site name or url")}, ["name_or_url"]),
    _schema("get_provider_status", "Which AI models Maki is using right now."),
    # V13 vision + screen control
    _schema("look_at_screen",
            "See the user's screen and answer a question about what's on it.",
            {"question": _S("question or task about the screen")}, ["question"]),
    _schema("describe_screen", "Describe what's currently on the screen."),
    _schema("read_text_on_screen", "Read the visible text on the screen."),
    _schema("scroll", "Scroll the active window.",
            {"direction": _S("up|down|left|right"),
             "amount":    {"type": "integer", "description": "wheel notches 1-20"}},
            ["direction"]),
    _schema("type_text", "Type text into the focused input.",
            {"text": _S("text to type")}, ["text"]),
    _schema("press_keys", "Press a key or chord like 'enter','ctrl+t','alt+left'.",
            {"combo": _S("key or chord")}, ["combo"]),
    _schema("click_at", "Click mouse at absolute screen pixel.",
            {"x": {"type":"integer"}, "y": {"type":"integer"},
             "double": _B("double-click")}, ["x","y"]),
    _schema("click_text", "Click a UI element described in plain words (vision).",
            {"target_label": _S("element description e.g. 'blue Submit button'")},
            ["target_label"]),
    _schema("browser_action",
            "Browser nav: back|forward|refresh|new_tab|close_tab|switch_tab|reopen_tab.",
            {"action": _S("action name")}, ["action"]),
    _schema("go_to_url", "Navigate browser to a URL.",
            {"url": _S("URL")}, ["url"]),
    _schema("invoke_ui_element",
            "Click a UI element by its accessible name (UIAutomation, more reliable than vision).",
            {"name": _S("element display name")}, ["name"]),
]


# ══════════════════════════════════════════════════════════════════════════════
# System prompt — defines Maki's personality + how to use tools
# ══════════════════════════════════════════════════════════════════════════════

_TODAY = _dt.date.today()
try:
    _DATE_LINE = _TODAY.strftime("%A, %B %d, %Y")
except Exception:
    _DATE_LINE = str(_TODAY)

AGENT_SYSTEM = f"""You are Maki — {config.USER_NAME}'s personal AI assistant living on his Windows PC.

TODAY'S DATE IS {_DATE_LINE}. THE CURRENT YEAR IS {_TODAY.year}.
Your training data is OLDER than today. Anything time-sensitive (current
rosters, latest news, "biggest" / "trending" / "right now" topics, prices,
scores, who-is-X-now) MUST be looked up with web_search — your built-in
knowledge is stale on these and WILL be wrong.
You are a real, thinking assistant. You can hold a natural conversation AND take
actions on the PC. You are NOT a rigid command bot.

HOW TO BEHAVE:
- Talk like a sharp, warm friend. Natural, brief, voice-friendly (1-3 sentences usually).
- THINK about what the user actually means before responding.
- If they're chatting, venting, or asking your opinion — just talk. No tools needed.
- If they ask for an action or a fact a tool can give — call the tool, then reply naturally
  using the result. Don't read raw tool output verbatim; phrase it like a person would.
- If they correct you ("you're wrong", "that's not it") — take it gracefully, ask what
  you got wrong, fix it. Never get defensive, never dump a status message.
- If you genuinely don't know and no tool helps — say so honestly and briefly, and offer
  to look it up. NEVER reply with "I'm rate-limited" or internal status as if it's an answer.
- Use multiple tools in sequence when needed (e.g. open an app, then maximize it).
- Keep answers SHORT for voice. No bullet lists, no preamble like "Certainly!".
- Treat each message on its OWN. If a message is short, garbled, or unclear,
  do NOT assume it continues the previous topic — and never claim you'll do
  something (like "let me check the time again") without actually calling the
  tool. If you truly can't tell what they mean, ask a brief clarifying question.

KNOWING WHEN TO SEARCH — and when you actually know (this matters):
- DO NOT INVENT facts, names, abbreviations, rosters, dates, or definitions.
  If you are not 90%+ certain, CALL web_search. Better to look it up than guess.
  (e.g. "HCC" most commonly means Houston Community College — DO NOT make up
  things like "Higher Certificate in Commerce".)
- web_search may return MULTIPLE sources in one response. READ them. Pick the
  one that actually answers the user's question — the first source isn't
  always the right one. (e.g. for "T1 LoL members", the Worlds Championship
  page is irrelevant; the "T1 (esports)" page has the roster.)
- If the first search doesn't find what's needed, REFINE the query and try
  again — add disambiguating words (the game, the team, the sport, etc.).
- If after a real search effort you still can't find it, say so honestly:
  "I checked but couldn't find a reliable answer." Don't make one up.

WRITING WEB_SEARCH QUERIES — read this carefully:
- DO NOT append a year (especially 2023, 2022, 2021) to your query unless
  the user specifically asked about that year. Adding a stale year hijacks
  the search into outdated results. WRONG: "biggest youtuber 2023".
  RIGHT: "biggest youtuber right now" or just "biggest youtuber".
- For "current / latest / right now / trending / news" — write the query
  with those very words. The search backend prefers fresh news results
  when it sees them, and won't get fooled by your training cutoff.
- If the user already says "current" or "latest", keep that wording in
  the query — DON'T translate it into a year tag.

SEEING THE SCREEN (V13 — you can actually see now):
- If the user says ANYTHING that implies looking at the screen — "look at my
  screen", "what's on my screen", "see this", "read this", "what does it
  say", "what's that error", "describe this", "check my screen", "see what
  I'm doing", "have a look", "look at this page", "what do you see" — you
  MUST call look_at_screen with their question. NEVER guess what's on screen.
- DO NOT apologize and say "technical issue" without first calling
  look_at_screen. CALL THE TOOL on the first try. Even if you think it might
  fail, try it. The vision pipeline has retries built in.
- For pure OCR ("read this for me") use read_text_on_screen.
- For free-form ("just describe what you see") use describe_screen.
- After seeing, answer based ONLY on what was returned. Don't invent details.
- For follow-ups about "their bio", "what does it mean", "summarize this" —
  call look_at_screen AGAIN with the new question. Each look_at_screen is a
  fresh capture; the previous one isn't in your memory.

ACTING ON THE SCREEN (V13 — you can also drive it now):
- Scroll: call scroll(direction='down'/'up', amount=N).
- Type into a focused field: call type_text(text=...).
- Hotkeys: call press_keys with combos like 'ctrl+t' (new tab),
  'ctrl+w' (close), 'alt+left' (back), 'win+d' (show desktop), 'enter', 'esc'.
- Browser nav (when a browser is focused): call browser_action with
  back/forward/refresh/new_tab/close_tab/switch_tab/reopen_tab — these are
  faster + more readable than press_keys.
- Open a URL: call go_to_url. Click something specific: prefer
  invoke_ui_element(name=...) for known apps; if that doesn't find it, fall
  back to click_text(target_label=...) which uses vision to locate it.
- Combine: if the user says "scroll down and tell me what you see", call
  scroll first, then look_at_screen. If they say "type X into the search
  bar", click_text('search bar') THEN type_text(X). Chain tools — that's
  the whole point.
- NEVER invent coordinates. Use click_text or invoke_ui_element instead of
  guessing pixel positions.
- "Click on the Mohammed Abbas profile" / "click that link" / "click the
  Send button" → call click_text(target_label="..."). If it fails (returns
  "couldn't find"), call look_at_screen to confirm the element exists,
  then retry with a refined description.

ACTING vs. CLAIMING — never lie about an action:
- If a user asks you to do something on the PC (focus a window, open an
  app, take a screenshot, change volume, etc.) you MUST call the matching
  tool. NEVER reply "Done" or "It's now in focus" without actually
  calling the tool — the user can see when nothing happened, and it
  destroys trust.
- For window focus / bring-to-front / switch-to-X requests, call
  control_window(action="focus", name=...). Do not just say it.

You know {config.USER_NAME} personally. Be present, be smart, be useful."""


# ══════════════════════════════════════════════════════════════════════════════
# Tool execution
# ══════════════════════════════════════════════════════════════════════════════

_CLOSE_INTENT_RE = re.compile(
    r"\b(close|quit|exit|kill|shut\s*(?:down|off)|terminate|end|stop\s+(?:the\s+)?(?:app|program|process))\b",
    re.I,
)
# Module-global: set by respond() each turn so _execute_tool can see the original user message
_current_user_text = ""


def _execute_tool(name: str, args: dict) -> str:
    fn = _TOOL_MAP.get(name)
    if not fn:
        return f"(unknown tool: {name})"
    # V14.6 SAFETY GUARD: refuse close_app unless user actually asked for it.
    # Stops the qwen3 catastrophe where "go to chrome" → close_app(Chrome).
    if name == "close_app":
        if not _CLOSE_INTENT_RE.search(_current_user_text or ""):
            logger.warning("BLOCKED close_app(%s) — user did not say close/quit. text=%r",
                           args, _current_user_text[:80])
            return ("(refused: I won't close an app unless you specifically say "
                    "'close' or 'quit')")
    try:
        result = fn(**(args or {}))
        logger.info("agent tool: %s(%s) -> %s", name, args, str(result)[:80])
        return str(result)
    except TypeError as e:
        logger.warning("agent tool %s bad args %s: %s", name, args, e)
        return f"(couldn't run {name} — {e})"
    except Exception as e:
        logger.warning("agent tool %s failed: %s", name, e)
        return f"(error running {name})"


# ══════════════════════════════════════════════════════════════════════════════
# Ollama agentic loop (manual tool-calling)
# ══════════════════════════════════════════════════════════════════════════════

def _ollama_agent(text: str, history: list) -> str:
    """V19 Step 2.5: thin wrapper holding the local-model slot so we don't
    OOM the 8 GB card by trying to load Hermes + qwen3-vl-4b at the same time."""
    from local_lock import local_model_slot
    with local_model_slot("hermes_agent"):
        return _ollama_agent_impl(text, history)


def _ollama_agent_impl(text: str, history: list) -> str:
    model = getattr(config, "_ollama_model_actual", None) or config.OLLAMA_MODEL
    try:
        import brain
        model = brain._ollama_model_actual or config.OLLAMA_MODEL
    except Exception:
        pass

    messages = [{"role": "system", "content": AGENT_SYSTEM}]
    for h in history[-10:]:
        messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
    messages.append({"role": "user", "content": text})

    last_content = ""
    last_tool_result = ""    # V14: track the most recent tool observation
    any_tool_ran = False     # V14: did any tool succeed during this turn?

    for round_i in range(3):                      # up to 3 tool rounds
        try:
            r = requests.post(_OLLAMA_CHAT_URL, json={
                "model":      model,
                "messages":   messages,
                "stream":     False,
                "tools":      _OLLAMA_SCHEMAS,
                "keep_alive": _KEEP_ALIVE,
                "think":      False,
            }, timeout=_AGENT_TIMEOUT)
            r.raise_for_status()
            msg = r.json().get("message", {}) or {}
        except requests.Timeout:
            logger.info("Ollama agent timed out (round %d).", round_i)
            # V14: if a tool already ran, don't drop the user into the graceful
            # fallback — speak the tool result. "Scrolled down" is much better
            # than "I'm here, give me a moment".
            if any_tool_ran and last_tool_result:
                return _humanize_tool_result(last_tool_result)
            return last_content or ""
        except Exception as e:
            logger.info("Ollama agent error: %s", e)
            if any_tool_ran and last_tool_result:
                return _humanize_tool_result(last_tool_result)
            return last_content or ""

        content   = (msg.get("content") or "").strip()
        tool_calls = msg.get("tool_calls") or []
        if content:
            last_content = content

        if not tool_calls:
            return content or last_content

        # Execute every requested tool, feed results back, loop for the final answer
        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        for tc in tool_calls:
            fn_blk = tc.get("function", {}) or {}
            fn_name = fn_blk.get("name", "")
            fn_args = fn_blk.get("arguments", {})
            if isinstance(fn_args, str):
                try: fn_args = json.loads(fn_args)
                except Exception: fn_args = {}
            result = _execute_tool(fn_name, fn_args)
            messages.append({"role": "tool", "name": fn_name, "content": result})
            if result and not result.startswith("(error") and not result.startswith("(unknown"):
                any_tool_ran = True
                last_tool_result = result

    # All 3 rounds exhausted — return what we have
    if last_content:
        return last_content
    if any_tool_ran and last_tool_result:
        return _humanize_tool_result(last_tool_result)
    return ""


def _humanize_tool_result(result: str) -> str:
    """V14: turn a raw tool-observation string into something voice-friendly when
    the LLM ran out of time to summarize it. Most tool results are already
    natural ("Scrolled down.", "Discord is now in focus.") so this is largely
    a passthrough — but we shorten/clean obvious junk."""
    r = (result or "").strip()
    if not r:
        return ""
    # Already a clean sentence — pass through
    return r[:280]


# ══════════════════════════════════════════════════════════════════════════════
# Gemini agentic loop (native automatic function calling)
# ══════════════════════════════════════════════════════════════════════════════

def _gemini_agent(text: str, history: list) -> str:
    import brain   # lazy — avoids circular import
    if not brain._can_use_gemini():
        return ""
    try:
        from google.genai import types
        client = brain._get_genai_client()
        g_hist = brain._to_gemini_history(history)
        cfg = types.GenerateContentConfig(
            system_instruction=AGENT_SYSTEM,
            tools=_PY_TOOLS,                       # SDK auto-calls these + loops
            temperature=0.6,
            max_output_tokens=400,
        )
        chat = client.chats.create(model=config.GEMINI_MODEL, config=cfg, history=g_hist)
        resp = chat.send_message(text)
        return (resp.text or "").strip()
    except Exception as e:
        brain._handle_gemini_error(e)
        logger.info("Gemini agent failed: %s", str(e)[:120])
        return ""


# ══════════════════════════════════════════════════════════════════════════════
# Public entry — the thinking pipeline
# ══════════════════════════════════════════════════════════════════════════════

# V15.3: regex to detect / strip raw tool-call JSON from LLM output.
# Cerebras (and others) sometimes emit `{"tool":"foo","args":...}` as plain
# text when they're told about tools but can't actually call them. Spoken
# aloud, that's gibberish. Strip it.
_JSON_TOOL_RE = re.compile(
    r'\{\s*"(?:tool|name|function|action|id|args|arguments)"\s*:\s*[^{}]*\}',
    re.I | re.S,
)
_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")


def _strip_tool_call_junk(reply: str) -> str:
    """Remove raw JSON tool calls + code blocks from a chat reply, return cleaned text."""
    if not reply: return ""
    out = reply
    # Remove fenced code blocks
    out = _CODE_BLOCK_RE.sub("", out)
    # Remove JSON tool-call patterns
    out = _JSON_TOOL_RE.sub("", out)
    # Clean up doubled whitespace and leading/trailing junk
    out = re.sub(r"\s{2,}", " ", out).strip()
    out = re.sub(r"^[\s,;\.\-]+", "", out)
    # If we stripped EVERYTHING, return a clarifying message instead
    if not out or len(out) < 2:
        return ("I tried to do that but my answer came out as code. "
                "Could you rephrase what you'd like?")
    return out


# V14.3: command-y keywords that ALWAYS need the tool-calling agent.
_COMMAND_KEYWORDS = re.compile(
    r"\b(?:open|close|focus|minimize|maximize|restore|quit|launch|start|run|"
    r"scroll|click|press|type|hit|tap|new\s+tab|back|forward|refresh|reload|"
    r"go\s+to|navigate|visit|"
    r"weather|temperature|time|date|"
    r"screen|screenshot|see|look|read|describe|"
    r"search|google|youtube|"
    r"app|window|tab|file|folder|disk|drive|space|size|"
    r"play|pause|stop|volume|mute|"
    r"discord|chrome|spotify|steam|browser|"
    r"recall|remember|memory|"
    r"convert|celsius|fahrenheit|"
    r"current|latest|news|today|now|right\s+now"
    r")\b",
    re.I,
)

def _is_chitchat(text: str) -> bool:
    """V14.3: short conversational turn with NO command/tool keywords."""
    if not text or len(text) > 140:
        return False
    if _COMMAND_KEYWORDS.search(text):
        return False
    # V14.4: also reject "command continuation" fragments — short utterances
    # that look like they're modifying the previous command ("10 times",
    # "all the way", "a hundred times", "more", "down", etc.). Letting
    # chitchat handle these makes qwen3 HALLUCINATE tool results.
    t_low = text.lower().strip()
    if re.match(r"^(?:\d+\s+times?|a?\s*hundred(?:\s+times?)?|all\s+the\s+way|"
                r"more|again|once\s+more|keep\s+going|do\s+it|do\s+that|"
                r"up|down|left|right|"
                r"to\s+the\s+(?:top|bottom|left|right))$", t_low):
        return False
    # Very short / no question words at all → chitchat
    wc = len(text.split())
    return wc <= 12


# Phrases that indicate the LLM hallucinated an action in a no-tools chat path
_FAKE_ACTION_RE = re.compile(
    r"\b(?:scrolled|clicked|pressed|typed|opened|closed|minimized|maximized|"
    r"navigated|switched|refreshed|reloaded|focused|brought\s+to\s+front)\b",
    re.I,
)


# V15.2: Cerebras provider — super-fast (500-700ms) cloud brain, free 1M tok/day
def _cerebras_chat(text: str, history: list, system: str | None = None) -> str:
    """Call Cerebras chat completions (OpenAI-compatible). Returns reply text or ''."""
    key = getattr(config, "CEREBRAS_API_KEY", "")
    if not key: return ""
    model = getattr(config, "CEREBRAS_MODEL", "gpt-oss-120b")
    sys_prompt = system or (
        f"You are Maki, {config.USER_NAME}'s warm, witty personal assistant. "
        f"Reply in 1-2 short sentences, voice-friendly. No bullet lists, no preambles."
    )
    messages = [{"role": "system", "content": sys_prompt}]
    for h in history[-6:]:
        messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
    messages.append({"role": "user", "content": text})
    # V19 Step 1: 8K context guard. Skip Cerebras silently if the projected
    # token count would blow the free-tier 8K window. Caller falls through
    # to Ollama (or next provider). Step 6 will reroute to NIM instead.
    try:
        from budget import would_overflow_cerebras
        if would_overflow_cerebras(messages):
            logger.info("agent: 8K guard tripped on Cerebras — falling through")
            return ""
    except Exception:
        pass
    try:
        r = requests.post(getattr(config, "CEREBRAS_URL",
                                   "https://api.cerebras.ai/v1/chat/completions"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages,
                  "max_completion_tokens": 300, "temperature": 0.6, "stream": False},
            timeout=getattr(config, "CEREBRAS_TIMEOUT", 10))
        if r.status_code != 200:
            logger.info("Cerebras HTTP %d: %s", r.status_code, r.text[:120])
            return ""
        d = r.json()
        msg = d.get("choices", [{}])[0].get("message", {})
        return (msg.get("content") or "").strip()
    except Exception as e:
        logger.info("Cerebras call failed: %s", e)
        return ""


def _ollama_chitchat(text: str, history: list) -> str:
    """V19 Step 2.5: wrap with the local-model slot."""
    from local_lock import local_model_slot
    with local_model_slot("hermes_chitchat"):
        return _ollama_chitchat_impl(text, history)


def _ollama_chitchat_impl(text: str, history: list) -> str:
    """V14.3: NO-tools Ollama call for pure conversational turns. Faster than
    the tool-calling loop because tools schema overhead is gone and we only
    do one round."""
    model = config.OLLAMA_MODEL
    try:
        import brain
        model = brain._ollama_model_actual or config.OLLAMA_MODEL
    except Exception:
        pass
    messages = [{"role": "system", "content": (
        f"You are Maki, {config.USER_NAME}'s warm, witty personal assistant. "
        f"Reply in 1-2 short sentences, voice-friendly. No bullet lists, no preambles. "
        f"You are NOT performing any task right now — just chatting briefly."
    )}]
    for h in history[-6:]:
        messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
    messages.append({"role": "user", "content": text})
    try:
        r = requests.post(_OLLAMA_CHAT_URL, json={
            "model": model, "messages": messages, "stream": False,
            "keep_alive": _KEEP_ALIVE, "think": False,
            "options": {"num_predict": 120, "temperature": 0.7},
        }, timeout=10)
        r.raise_for_status()
        return (r.json().get("message", {}).get("content") or "").strip()
    except Exception as e:
        logger.info("chitchat path failed: %s", e)
        return ""


def respond(text: str) -> str:
    """
    Run the agentic brain on a user message. Returns Maki's natural reply.

    V19 routing (NEW — runs BEFORE the V18 fallback chain):
      1. Lane classifier picks one of {groq_8b, cerebras_120b, github_premium,
         nim_nemotron, hermes_tools, vision} based on intent + Think mode.
      2. Cloud-chat lanes (groq, github, cerebras, nim) call their providers
         via lane_dispatch. The dispatcher has its own fallback chain.
      3. `hermes_tools` and `vision` fall through to the existing V18 paths
         (Hermes agentic loop / intent_router handles vision).
      4. If V19 dispatch returns empty for any reason, V18 logic runs as a
         safety net so a V19 bug never kills responses.
    """
    global _current_user_text
    _current_user_text = text or ""
    history = memory.get_history()

    # ── V19 lane routing (wired into runtime) ───────────────────────────────
    try:
        import lane_classifier as _lc
        import lane_dispatch  as _ld
        import brain          as _br
        import memory         as _mem

        _think_on = False
        try: _think_on = _mem.is_think_mode()
        except Exception: pass

        _router = getattr(_br, "_intent_router", None)
        _lane, _info = _lc.select_lane(text, think_mode_on=_think_on, router=_router)
        _lc.log_decision(_info)
        logger.info("V19 lane: %r -> %s (reason=%s, intent=%s, conf=%s)",
                    text[:60], _lane, _info.get("reason"),
                    _info.get("intent"), _info.get("intent_conf"))

        # hermes_tools and vision: fall through to existing V18 handlers
        if _lane not in ("hermes_tools", "vision"):
            # Think mode (github_premium) gets a permissive prompt — the whole
            # point is hard reasoning / code. Other lanes get the voice-friendly
            # cap because they're spoken aloud verbatim.
            if _lane == "github_premium":
                _system = (
                    f"You are Maki, {config.USER_NAME}'s personal AI assistant. "
                    f"Think mode is ON — the user asked for deep reasoning. Give "
                    f"a complete answer: explanations can be long, code blocks "
                    f"are welcome, structured output is fine. The user can read "
                    f"the screen — this won't be spoken verbatim. Be thorough."
                )
            else:
                _system = (
                    f"You are Maki, {config.USER_NAME}'s warm personal AI assistant on "
                    f"Windows. Reply in plain English, 1-3 short sentences, voice-friendly. "
                    f"NEVER emit JSON, function calls, raw tool names, or code blocks. "
                    f"For genuine questions or chitchat, just answer directly."
                )
            _reply, _dinfo = _ld.dispatch(text, history, _lane, system=_system)
            if _reply:
                # Strip JSON tool-call junk ONLY for voice-lanes. github_premium
                # is allowed to emit code blocks (the whole point of Think mode).
                if _lane != "github_premium":
                    _reply = _strip_tool_call_junk(_reply) or _reply
                logger.info("V19 lane: answered via %s (requested %s, fallback=%s)",
                            _dinfo.get("lane_used"), _lane, _dinfo.get("fallback"))
                return _reply
            logger.info("V19 lane: %s returned empty — falling through to V18 path", _lane)
    except Exception as _e:
        logger.warning("V19 lane routing skipped (%s) — using V18 path", _e)

    # V14.3: short chitchat with no command keywords → bypass the tool-calling
    # agent entirely. Tools schema + multi-round loop costs ~5-10s; chitchat
    # without it is ~2-4s on local Ollama.
    try:
        import brain as _b
        gem_down = not _b._can_use_gemini()
    except Exception:
        gem_down = True
    if _is_chitchat(text):
        # V15.2: try Cerebras FIRST (500-700ms, smartest, free 1M tok/day).
        chat = _cerebras_chat(text, history)
        chat = _strip_tool_call_junk(chat) if chat else ""
        if chat and not _FAKE_ACTION_RE.search(chat):
            logger.info("agent: chitchat via Cerebras (fast)")
            return chat
        if chat and _FAKE_ACTION_RE.search(chat):
            logger.warning("agent: Cerebras hallucinated action — falling through: %s", chat[:100])

        # Fallback: local Ollama chitchat (only if Gemini also unavailable)
        if gem_down:
            chat = _ollama_chitchat(text, history)
            if chat and not _FAKE_ACTION_RE.search(chat):
                logger.info("agent: chitchat fast-path via Ollama (no tools)")
                return chat
            if chat and _FAKE_ACTION_RE.search(chat):
                logger.warning("agent: chitchat hallucinated action — falling through.")

    # 1. Gemini — best reasoning, native tool-calling
    try:
        import brain
        gemini_ok = brain._can_use_gemini()
    except Exception:
        gemini_ok = False
    if gemini_ok:
        reply = _gemini_agent(text, history)
        if reply:
            logger.info("agent: answered via Gemini")
            return reply
        logger.info("agent: Gemini empty/failed — trying Ollama")

    # 2. V15.2/V17: Cerebras — cloud, ~600ms, free 1M tok/day.
    # V17 FIX: previous prompt said "you have NO tools" so Cerebras refused
    # even things Maki could do ("I can't type for you" while type_text exists).
    # Now we tell it what Maki CAN do via the rest of the pipeline, and instruct
    # it to suggest the correct command phrasing instead of refusing.
    _CEREBRAS_AGENT_SYSTEM = (
        f"You are Maki, {config.USER_NAME}'s warm personal AI assistant on Windows. "
        f"Reply in plain English, 1-3 short sentences, voice-friendly. NEVER emit "
        f"JSON, function calls, raw tool names, or code blocks.\n\n"
        f"Maki CAN do these things (via separate handlers you don't call directly):\n"
        f"• Open / focus / close apps (Chrome, Discord, Spotify, etc.)\n"
        f"• Scroll, click on UI elements by name, type text, press keys (Ctrl+T, Enter, etc.)\n"
        f"• Browser navigation (new tab, close tab, go back, go to URL)\n"
        f"• Take a screenshot, look at the screen (vision), read on-screen text\n"
        f"• Weather, time, world time, web search, file/disk info\n"
        f"• Window control (minimize, maximize)\n\n"
        f"If the user asks for one of these, DON'T refuse — instead, briefly tell "
        f"them how to phrase it (e.g. \"Just say 'type google studio in the search "
        f"bar' and I'll do it.\"). For genuine questions or chitchat, just answer."
    )
    reply = _cerebras_chat(text, history, system=_CEREBRAS_AGENT_SYSTEM)
    if reply:
        cleaned = _strip_tool_call_junk(reply)
        if cleaned:
            logger.info("agent: answered via Cerebras (%s)",
                        getattr(config, "CEREBRAS_MODEL", "gpt-oss-120b"))
            return cleaned

    # 3. Ollama qwen3 — local, capable, kept warm in VRAM (last resort if Cerebras down)
    reply = _ollama_agent(text, history)
    if reply:
        logger.info("agent: answered via Ollama qwen3")
        return reply

    # 4. Graceful fallback — a real reply, never a status dump
    logger.info("agent: all LLMs unavailable — graceful fallback")
    return _graceful_fallback(text)


_FALLBACK_RE_GREETING = re.compile(r"^(hi+|hey+|hello+|yo|sup|good (morning|evening|afternoon))\b", re.I)
_FALLBACK_RE_THANKS   = re.compile(r"\b(thanks|thank you|appreciate)\b", re.I)
_FALLBACK_RE_FEELING  = re.compile(r"\b(how are you|what.?s up|how.?s it going)\b", re.I)


def _graceful_fallback(text: str) -> str:
    """Last-resort reply when no LLM is reachable — still conversational, never a status dump."""
    t = text.lower().strip()
    if _FALLBACK_RE_GREETING.search(t):
        return f"Hey {config.USER_NAME}. I'm here — what do you need?"
    if _FALLBACK_RE_THANKS.search(t):
        return "Anytime."
    if _FALLBACK_RE_FEELING.search(t):
        return "I'm good and ready to help. What's on your mind?"
    if "?" in text or re.match(r"^(what|who|why|how|when|where|is|are|can|do)\b", t):
        return ("My thinking model is briefly unreachable, so I can't reason that one out "
                "fully right now — but I can still handle time, apps, weather, screenshots "
                "and more. Want me to try one of those, or ask again in a moment?")
    return "I'm here. Give me a moment if my reasoning feels slow — what would you like to do?"


# ── Keep Ollama warm ─────────────────────────────────────────────────────────

def prewarm_ollama() -> None:
    """Load qwen3 into VRAM at boot and keep it resident so it never cold-starts."""
    def _warm():
        model = config.OLLAMA_MODEL
        try:
            import brain
            model = brain._ollama_model_actual or config.OLLAMA_MODEL
        except Exception:
            pass
        # V14.2 safety: never pre-warm a vision/embed model as the chat brain
        if any(b in model.lower() for b in ("-vl", "vl:", "embed", "moondream",
                                             "minicpm-v", "llava", "vision")):
            logger.warning("prewarm_ollama: refusing to warm vision/embed model '%s' "
                           "— falling back to config.OLLAMA_MODEL '%s'",
                           model, config.OLLAMA_MODEL)
            model = config.OLLAMA_MODEL
        # V19 Step 2.5: hold the local-model slot during pre-warm so we don't
        # race with vision pre-warm (which also runs at boot).
        try:
            from local_lock import local_model_slot
            slot_cm = local_model_slot("hermes_prewarm")
        except Exception:
            from contextlib import nullcontext
            slot_cm = nullcontext()
        with slot_cm:
            try:
                requests.post(
                    getattr(config, "OLLAMA_URL", "http://localhost:11434/api/chat"),
                    json={"model": model, "messages": [{"role": "user", "content": "hi"}],
                          "stream": False, "keep_alive": _KEEP_ALIVE},
                    timeout=60,
                )
                logger.info("Ollama '%s' pre-warmed and resident (keep_alive=%s).",
                            model, _KEEP_ALIVE)
            except Exception as e:
                logger.info("Ollama pre-warm skipped: %s", e)
    import threading
    threading.Thread(target=_warm, daemon=True, name="ollama-prewarm").start()
