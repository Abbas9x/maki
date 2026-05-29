"""
intents.py — V15 declarative intent definitions.

Each intent has:
  - name        — unique id
  - examples    — natural ways users actually phrase this command
  - handler(text) → str  — the action; returns the spoken reply (or None to skip)
  - threshold   — minimum cosine sim (default 0.65). Tighter (0.78+) for
                  destructive actions (close, delete).
  - negatives   — phrases that LOOK similar but are NOT this intent
                  (used to disambiguate open vs close, scroll up vs down, etc.)

Adding a new intent = add one Intent() entry. NO regex.
"""

from __future__ import annotations
import logging, re
from typing import Optional

from intent_router import Intent, IntentRouter

logger = logging.getLogger(__name__)


# ── Handler helpers ─────────────────────────────────────────────────────────
def _sc():
    import screen_control; return screen_control
def _wt():
    import window_tools; return window_tools
def _vt():
    import vision_tools; return vision_tools
def _act():
    import actions; return actions
def _wth():
    import weather_tools; return weather_tools
def _tt():
    import tools; return tools
def _wtt():
    import world_time_tools; return world_time_tools


# Tiny extractor: word amounts → ints
_WORDNUM = {"one":1,"a":1,"once":1,"two":2,"twice":2,"three":3,"thrice":3,
            "four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9,"ten":10,
            "twenty":20,"thirty":30,"forty":40,"fifty":50,"hundred":100}

def _parse_count(text: str, default: int = 5) -> int:
    m = re.search(r"\b(\d{1,3})\b", text)
    if m:
        try: return max(1, min(int(m.group(1)), 100))
        except Exception: pass
    for w in ("hundred","fifty","forty","thirty","twenty","ten","nine","eight",
              "seven","six","five","four","thrice","three","twice","two","once","one"):
        if re.search(rf"\b{w}\b", text.lower()): return _WORDNUM[w]
    return default


def _extract_app(text: str) -> str:
    """V15.3: Aggressively strip filler so any natural phrasing → 'chrome'.
    Handles: 'bring chrome to front', 'focus on chrome', 'my chrome',
             'take me to chrome', 'pull up chrome', etc."""
    t = (text or "").lower().strip()
    t = re.sub(r"^hey\s+maki[,\s]+", "", t)
    # Strip leading politeness
    t = re.sub(r"^(?:please|can\s+you|could\s+you|would\s+you|will\s+you)\s+", "", t)
    # Strip every common verb-phrase prefix (multi-pass — handles "please bring chrome to front")
    for _ in range(4):
        before = t
        t = re.sub(
            r"^(?:"
            r"go\s+(?:to|over\s+to)|switch\s+(?:to|over\s+to)|take\s+me\s+to|"
            r"jump\s+to|show\s+me|bring\s+(?:up|me\s+to|to\s+(?:the\s+)?front)|"
            r"bring|"
            r"open\s+up|open|launch|fire\s+up|start|boot\s+up|load\s+up|pull\s+up|"
            r"give\s+me|get\s+me\s+(?:on|to)|"
            r"focus\s+(?:on)?|"
            r"close|quit|exit|kill|shut\s+down|terminate|end|"
            r"minimize|minimise|hide|"
            r"maximize|maximise|expand|fullscreen"
            r")\s+", "", t)
        t = re.sub(r"^(?:the|a|an|my|some)\s+", "", t)
        if t == before: break   # nothing changed → done stripping
    # Strip trailing fluff
    t = re.sub(
        r"\s+(?:please|for\s+me|now|right\s+now|app|application|window|"
        r"to\s+(?:the\s+)?front|forward|up|down|on\s+(?:my\s+)?screen)\s*$",
        "", t,
    )
    return t.strip().rstrip(".,!?;:")


def _extract_city(text: str) -> str:
    """V17.2: robust city extraction. Strips unit prefixes ('show the weather
    in celsius for X'), pronouns won't reach here (handler rejects upstream).
    """
    t = text.lower()
    # V17.2: pre-strip the converted-perception output patterns so we get the
    # actual city list out of "show the weather in celsius for Karachi, Egypt"
    t = re.sub(r"^(?:show|give|tell)\s+(?:me\s+)?the\s+weather\s+", "weather ", t)
    t = re.sub(r"\bin\s+celsius\s+for\s+", "for ", t)
    t = re.sub(r"\bin\s+fahrenheit\s+for\s+", "for ", t)

    # Now extract city/cities. Accept "in X" OR "for X" OR "of X"
    m = re.search(r"\b(?:in|for|of)\s+(.+?)(?:\?|$|\s+in\s+(?:celsius|fahrenheit|c|f))", t)
    if m:
        city = m.group(1).strip().rstrip(".,!?")
        # Strip trailing unit qualifiers
        city = re.sub(r"\s+(?:in\s+(?:celsius|fahrenheit|c|f)|please|right\s+now)\s*$",
                      "", city).strip()
        return city
    return ""


# ══════════════════════════════════════════════════════════════════════════
# Intent handlers
# ══════════════════════════════════════════════════════════════════════════

# V17: known app names — if extracted target isn't one of these (or doesn't
# resolve via app_index), REFUSE to "open" it. Stops the catastrophe of
# "can you create a website" → "Trying to open Create A Website".
_KNOWN_APP_NAMES = {
    "chrome","google chrome","firefox","brave","edge","opera",
    "discord","slack","teams","zoom","whatsapp","telegram",
    "spotify","steam","epic games","league of legends","minecraft","valorant","roblox",
    "vscode","visual studio code","vs code","notepad","wordpad","calculator",
    "explorer","file explorer","files","photos","camera",
    "outlook","gmail","mail","calendar","clock",
    "settings","control panel","cmd","powershell","terminal","windows terminal",
    "obs","obs studio","figma","photoshop","illustrator","blender","unity",
    "notion","obsidian","onenote","word","excel","powerpoint",
    "github","github desktop","youtube","netflix","twitch",
    "claude","chatgpt","perplexity","gemini","copilot",
    "maki","task manager",
}

_NON_APP_VERBS = {"type","search","make","create","write","find","look","get",
                    "tell","show","describe","explain","know","do","play","check",
                    "google","scroll","click","press","tap","read","see"}


def _is_known_app(name: str) -> bool:
    n = (name or "").lower().strip()
    if not n: return False
    if n in _KNOWN_APP_NAMES: return True
    # V17 SAFETY: if the phrase starts with a verb that's NOT an app name,
    # refuse — stops "type google studio" → "chrome" fuzzy match.
    first_word = n.split()[0]
    if first_word in _NON_APP_VERBS:
        return False
    # app_index fuzzy match — but require the match to actually appear in `n`
    try:
        import app_index
        if app_index:
            r = app_index.resolve(n)
            if r and r.get("confidence", 0) >= 0.85:
                match = (r.get("match") or "").lower()
                # The matched app name should appear (or its base) in the user's words
                if match and (match in n or any(w in match for w in n.split())):
                    return True
    except Exception: pass
    # Single word + common app pattern
    if len(n.split()) == 1 and re.match(r"^[a-z][a-z0-9]{2,15}$", n):
        return True
    return False


def _distill_app_name(phrase: str) -> Optional[str]:
    """V17.1/V17.2: when extracted phrase contains a KNOWN app name plus noise
    ('brain chrome', 'gmail in google', 'ring chrome to front'), return just
    the most-likely target app. Catches Whisper mishearings."""
    if not phrase: return None
    p = phrase.lower().strip()
    # Direct hit
    if p in _KNOWN_APP_NAMES: return p
    # V17.2: "X in Y" pattern — if both X and Y are known, prefer X (the target)
    m = re.match(r"^(\S+)\s+in\s+(\S+(?:\s+\S+)?)$", p)
    if m:
        a, b = m.group(1), m.group(2)
        a_known = a in _KNOWN_APP_NAMES
        b_known = (b in _KNOWN_APP_NAMES) or any(b in app for app in _KNOWN_APP_NAMES)
        if a_known and b_known: return a   # "gmail in google" → gmail
        if a_known: return a
        if b_known and b in _KNOWN_APP_NAMES: return b

    tokens = re.findall(r"[a-z][a-z0-9]+", p)
    # Multi-word app names first
    for app in sorted(_KNOWN_APP_NAMES, key=lambda x: -len(x)):
        if " " in app and app in p:
            return app
    # Single-word app names
    for tok in tokens:
        if tok in _KNOWN_APP_NAMES:
            return tok
    return None


def h_focus_app(text: str) -> Optional[str]:
    app = _extract_app(text)
    if not app: return None
    # V17 SAFETY GATE: don't blindly try to "open" arbitrary phrases
    if not _is_known_app(app):
        logger.info("h_focus_app: refusing unknown target %r (text=%r)", app, text)
        return None   # fall through — let perception/agent handle it
    # V17.1: distill the actual app name from noisy phrases.
    # "brain chrome" → "chrome", "ring chrome to front" → "chrome"
    distilled = _distill_app_name(app)
    if distilled and distilled != app:
        logger.info("h_focus_app: distilled %r → %r", app, distilled)
        app = distilled
    try:
        r = _wt().focus_window(app)
        if r and "error" not in r:
            return f"Brought {_clean_title(r.get('title', app.title()))} to front."
    except Exception: pass
    try:
        return _act().open_app(app)
    except Exception: return None


_APP_SUFFIXES = ("Google Chrome", "Mozilla Firefox", "Microsoft Edge",
                  "Brave", "Discord", "Slack", "Visual Studio Code",
                  "Notepad", "Spotify", "Steam", "WhatsApp",
                  "Telegram", "Microsoft Word", "Microsoft Excel",
                  "Microsoft PowerPoint", "Notion", "Obsidian",
                  "OBS Studio", "Photoshop", "VLC")


def _clean_title(title: str) -> str:
    """V17.2: strip browser-tab clutter for voice. Handles multi-dash titles
    like 'gmail in google - Google Search - Google Chrome' → 'Google Chrome'."""
    if not title: return ""
    t = title.strip()
    # V17.2: if ANY known app suffix appears at the very END (after the LAST
    # dash), return just that suffix. Works even when title has many dashes.
    for suffix in _APP_SUFFIXES:
        # Match " - <suffix>" or " — <suffix>" anywhere, take the LAST
        # boundary so multi-dash titles collapse properly.
        if t.endswith(" - " + suffix) or t.endswith(" — " + suffix):
            return suffix
        if t == suffix:
            return suffix
    # Strip leading "@username - "
    t = re.sub(r"^@\w+\s*[-—]\s*", "", t)
    # Strip URL prefixes
    t = re.sub(r"^https?://\S+\s*[-—]\s*", "", t)
    return t[:60]

def h_open_app(text: str) -> Optional[str]:
    app = _extract_app(text)
    if not app: return None
    try: return _act().open_app(app)
    except Exception: return None

def h_close_app(text: str) -> Optional[str]:
    app = _extract_app(text)
    if not app: return None
    try: return _act().close_app(app)
    except Exception: return None

def h_minimize(text: str) -> Optional[str]:
    app = _extract_app(text)
    if not app: return None
    r = _wt().minimize_window(app)
    if "error" in r: return r["error"]
    return f"Minimized {r.get('title', app.title())}."

def h_maximize(text: str) -> Optional[str]:
    app = _extract_app(text)
    if not app: return None
    r = _wt().maximize_window(app)
    if "error" in r: return r["error"]
    return f"Maximized {r.get('title', app.title())}."

def h_scroll(text: str) -> Optional[str]:
    t = text.lower()
    direction = "down"
    if re.search(r"\b(up|upward|upwards|north)\b", t): direction = "up"
    elif re.search(r"\b(right)\b", t): direction = "right"
    elif re.search(r"\b(left)\b", t): direction = "left"
    amount = _parse_count(text, default=5)
    return _sc().scroll(direction, amount)

def h_new_tab(text: str) -> Optional[str]:
    """V17.2: support multi-tab ('open 3 tabs', 'create five tabs')."""
    import time as _t
    n = 1
    m = re.search(r"\b(\d{1,2})\s+(?:tabs?|new\s+tabs?)\b", text.lower())
    if m:
        try: n = max(1, min(int(m.group(1)), 20))
        except Exception: n = 1
    else:
        _wn = {"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,
                "eight":8,"nine":9,"ten":10}
        for w, v in _wn.items():
            if re.search(rf"\b{w}\s+(?:tabs?|new\s+tabs?)\b", text.lower()):
                n = v; break
    if n == 1:
        return _sc().press_keys("ctrl+t")
    for i in range(n):
        _sc().press_keys("ctrl+t")
        _t.sleep(0.08)
    return f"Opened {n} new tabs."
def h_close_tab(text: str) -> Optional[str]: return _sc().press_keys("ctrl+w")
def h_reopen_tab(text: str) -> Optional[str]: return _sc().press_keys("ctrl+shift+t")
def h_switch_tab(text: str) -> Optional[str]: return _sc().press_keys("ctrl+tab")
def h_back(text: str) -> Optional[str]:    return _sc().press_keys("alt+left")
def h_forward(text: str) -> Optional[str]: return _sc().press_keys("alt+right")
def h_refresh(text: str) -> Optional[str]: return _sc().press_keys("f5")

def h_select_all(text: str) -> Optional[str]:
    return _sc().press_keys("ctrl+a")

def h_copy(text: str) -> Optional[str]:  return _sc().press_keys("ctrl+c")
def h_paste(text: str) -> Optional[str]: return _sc().press_keys("ctrl+v")
def h_cut(text: str) -> Optional[str]:   return _sc().press_keys("ctrl+x")
def h_undo(text: str) -> Optional[str]:  return _sc().press_keys("ctrl+z")
def h_redo(text: str) -> Optional[str]:  return _sc().press_keys("ctrl+y")

def h_clear_field(text: str) -> Optional[str]:
    return _sc().press_keys("ctrl+a then delete")

def h_look_at_screen(text: str) -> Optional[str]:
    return _vt().look_at_screen(text)

def h_describe_screen(text: str) -> Optional[str]:
    return _vt().describe_screen()

def h_read_screen(text: str) -> Optional[str]:
    return _vt().read_text_on_screen()

def h_screenshot(text: str) -> Optional[str]:
    import screenshot_tools
    r = screenshot_tools.take_screenshot_to_clipboard()
    if "error" in r: return r["error"]
    return "Screenshot copied to your clipboard."

def h_click_element(text: str) -> Optional[str]:
    # extract target after click/press verb
    m = re.match(r"^(?:please\s+|can\s+you\s+)?"
                 r"(?:click|press|tap|select|hit)\s+(?:on\s+)?(?:the\s+)?(.+)$",
                 text.lower())
    if not m: return None
    target = m.group(1).strip().rstrip(".,!?")
    # strip noise suffixes
    target = re.sub(r"\s+(?:button|link|icon|tab|profile|option|item)$", "", target)
    if len(target) < 2: return None
    return _sc().click_text(target)

def h_click_center(text: str) -> Optional[str]:
    sw, sh = _sc().get_screen_size()
    return _sc().click_at(sw // 2, sh // 2)

def h_get_time(text: str) -> Optional[str]:
    import tools
    return f"It's {tools.get_current_time()}."

def h_get_date(text: str) -> Optional[str]:
    import tools
    return f"Today is {tools.get_current_date()}."

def h_time_in_place(text: str) -> Optional[str]:
    m = re.search(r"\bin\s+(.+?)(?:\?|$|\s+right\s+now)", text.lower())
    if not m: return None
    place = m.group(1).strip().rstrip(".,!?")
    if " and " in place or "," in place:
        places = re.split(r"\s*(?:,|\band\b)\s*", place)
        return " ".join(_wtt().speak_time_in(p.strip()) for p in places if p.strip())
    return _wtt().speak_time_in(place)

_PRONOUN_TARGETS_RE = re.compile(
    r"\b(?:them|those|these|all\s+(?:of\s+)?(?:them|those|the(?:m|se)?)|"
    r"all\s+(?:of\s+)?those\s+(?:countries|cities|places|locations)|"
    r"the\s+(?:same|previous)\s+(?:ones?|cities|countries))\b",
    re.I,
)


def h_get_weather(text: str) -> Optional[str]:
    city = _extract_city(text)
    if not city: return None
    # V17.1: reject pronoun-only targets — perception should have expanded
    # them. If it didn't, fall through so the agent can handle it.
    if _PRONOUN_TARGETS_RE.search(city):
        logger.info("h_get_weather: pronoun target %r — fall through", city)
        return None
    if " and " in city or "," in city:
        cities = [c.strip() for c in re.split(r"\s*(?:,|\band\b)\s*", city) if c.strip()]
        parts = []
        last_ok = None
        for c in cities[:5]:
            r = _wth().get_weather(c)
            if "error" in r: parts.append(f"Couldn't find weather for {c.title()}.")
            else: parts.append(r["summary"]); last_ok = r
        try:
            if last_ok:
                import memory
                memory.set_last_weather(float(last_ok["temp"]),
                                        "F" if "°F" in last_ok.get("unit","") else "C",
                                        last_ok.get("location", cities[-1]))
        except Exception: pass
        return " ".join(parts) if parts else "Couldn't check those cities."
    r = _wth().get_weather(city)
    if "error" in r: return f"I couldn't get weather for {city.title()}."
    try:
        import memory
        memory.set_last_weather(float(r["temp"]),
                                "F" if "°F" in r.get("unit","") else "C",
                                r.get("location", city))
    except Exception: pass
    return r["summary"]

def h_search_youtube(text: str) -> Optional[str]:
    m = re.search(r"(?:search|find|look\s+up|look\s+for)\s+(.+?)(?:\s+on\s+youtube|$)",
                  text.lower())
    if not m: return None
    q = m.group(1).strip()
    _tt().search_youtube(q)
    return f"Searching YouTube for '{q}'."

def h_search_google(text: str) -> Optional[str]:
    """V17.2: extract a clean search query. 'search the web for X' → 'X'.
    Previously yielded 'the web for X' as the query."""
    t = text.lower().strip()
    # Strip leading "search the web for / google / find" patterns
    t = re.sub(r"^(?:please\s+|can\s+you\s+)?", "", t)
    t = re.sub(r"^(?:hey\s+maki[,\s]+)?", "", t)
    # First-level verb strip
    m = re.match(
        r"^(?:search|google|find|look\s+up|look\s+for|web\s+search\s+for|"
        r"search\s+the\s+web\s+for|google\s+search\s+for)\s+(.+)$", t)
    if not m: return None
    q = m.group(1).strip()
    # Strip leading "the web for", "for ", "on google", etc.
    q = re.sub(r"^(?:the\s+web\s+for|for|on\s+google|on\s+the\s+web)\s+", "", q)
    q = re.sub(r"\s+(?:on\s+google|on\s+the\s+web|please)\s*$", "", q)
    q = q.strip(".,!?;: ")
    if not q: return None
    import web_tools
    return web_tools.open_google_search(q)


# ══════════════════════════════════════════════════════════════════════════
# Build the router
# ══════════════════════════════════════════════════════════════════════════

def build_router() -> IntentRouter:
    r = IntentRouter()

    # V15.1: focus_app and open_app do the SAME thing (focus if running, else
    # open). Collapse into one intent with broad examples to catch all natural
    # phrasings.
    r.register(Intent("focus_app", [
        # focus variants
        "go to chrome", "switch to chrome", "take me to chrome",
        "bring chrome to front", "bring up chrome", "show me chrome",
        "jump to chrome", "switch over to chrome", "give me chrome",
        "pull up chrome", "focus chrome", "focus on chrome", "my chrome",
        # open variants — same handler
        "open chrome", "open discord", "open spotify", "open youtube",
        "open league of legends", "open whatsapp", "open gmail",
        "launch discord", "start steam", "fire up vscode",
        "boot up the browser", "open up notepad", "start chrome",
        "load up chrome", "get me on chrome",
        # "google chrome" style
        "google chrome", "the browser", "chrome browser",
    ], h_focus_app, threshold=0.78, negatives=[   # V17: 0.65 → 0.78
        "close chrome", "quit chrome", "exit chrome", "kill chrome",
        "minimize chrome", "hide chrome",
        # creative prompts that mention an app name but aren't commands
        "write a poem about chrome", "tell me about chrome",
        "what is chrome", "who made chrome", "tell me a fact about chrome",
        # questions that should NEVER match focus_app (V17 fixes)
        "can you create a website for me", "create a website",
        "in the background", "what apps are running",
        "search youtube on google", "open it on youtube open it on google",
        "search google studio", "type google studio", "white google studio",
        "make a new tab and search google studio",
        "what's the weather", "what time is it",
        # generic question patterns
        "can you", "could you", "would you", "should i",
        "what is", "what are", "who is", "where is", "when is",
        "how do i", "how to", "explain", "describe",
        # V17.1 specific mishearings caught in log
        "brain chrome", "ring chrome to front",
        "and use this on google chrome",
        "i'll put in whatsapp and google chrome",
        "what are you doing right now",
        "what will be the day on 29th of may",
        "what day is the 29th",
        "what's the day on may 29",
    ]))

    r.register(Intent("close_app", [
        "close chrome", "quit discord", "exit spotify", "kill the process",
        "shut down chrome", "close the chrome app", "terminate discord",
    ], h_close_app, threshold=0.78, negatives=[
        "go to chrome", "open chrome", "switch to chrome",
        "close the tab", "close the window",
    ]))

    r.register(Intent("minimize_window", [
        "minimize chrome", "minimize this window", "hide discord",
        "minimize the browser",
    ], h_minimize, threshold=0.68, negatives=[
        "maximize chrome", "close chrome", "open chrome",
    ]))

    r.register(Intent("maximize_window", [
        "maximize chrome", "maximize discord", "make this window full screen",
        "expand the browser",
    ], h_maximize, threshold=0.68))

    r.register(Intent("scroll", [
        "scroll down", "scroll up", "scroll down a bit", "scroll down ten times",
        "scroll up twice", "scroll down a hundred times", "page down",
        "keep scrolling down", "scroll to the bottom", "scroll all the way up",
        "scroll up upwards", "scroll left", "scroll right",
    ], h_scroll, threshold=0.62, negatives=[
        "close chrome", "open chrome", "open the search bar",
    ]))

    r.register(Intent("new_tab", [
        "open a new tab", "new tab", "create a new tab", "add a tab",
        "open new tab in chrome", "make a new tab",
    ], h_new_tab, threshold=0.70, negatives=[
        "close tab", "close the tab", "switch tab",
    ]))

    r.register(Intent("close_tab", [
        "close tab", "close this tab", "close the current tab",
        "close the tab", "ctrl w",
    ], h_close_tab, threshold=0.70, negatives=[
        "open a new tab", "reopen tab",
    ]))

    r.register(Intent("reopen_tab", [
        "reopen the last closed tab", "reopen tab", "bring back the tab",
        "undo close tab", "restore the tab i just closed",
    ], h_reopen_tab, threshold=0.72))

    r.register(Intent("switch_tab", [
        "switch tab", "next tab", "go to the next tab", "ctrl tab",
    ], h_switch_tab, threshold=0.72))

    r.register(Intent("browser_back", [
        "go back", "navigate back", "previous page", "back",
        "go to the previous page", "alt left",
    ], h_back, threshold=0.68))

    r.register(Intent("browser_forward", [
        "go forward", "next page", "forward", "go to the next page",
    ], h_forward, threshold=0.70))

    r.register(Intent("refresh", [
        "refresh", "reload", "refresh the page", "reload this page",
        "f5",
    ], h_refresh, threshold=0.72))

    r.register(Intent("select_all", [
        "select all", "select everything", "select the whole thing",
        "highlight everything", "ctrl a",
    ], h_select_all, threshold=0.72))

    r.register(Intent("copy", [
        "copy", "copy that", "copy it", "copy this", "ctrl c",
    ], h_copy, threshold=0.74, negatives=[
        "cut that", "paste it",
    ]))

    r.register(Intent("paste", [
        "paste", "paste it", "paste here", "ctrl v",
    ], h_paste, threshold=0.78, negatives=[
        "copy that",
    ]))

    r.register(Intent("cut", [
        "cut", "cut it", "cut that", "ctrl x",
    ], h_cut, threshold=0.78))

    r.register(Intent("undo", [
        "undo", "undo that", "undo it", "ctrl z", "take that back",
    ], h_undo, threshold=0.76))

    r.register(Intent("redo", [
        "redo", "redo it", "redo that", "ctrl y", "ctrl shift z",
    ], h_redo, threshold=0.78))

    r.register(Intent("clear_field", [
        "clear the search bar", "clear the input", "clear the text",
        "empty the search box", "delete everything in this field",
    ], h_clear_field, threshold=0.72))

    r.register(Intent("look_at_screen", [
        "look at my screen", "what's on my screen", "what do you see on my screen",
        "see what's on my screen", "tell me what's on my screen",
        "what is on my screen", "check my screen", "analyze my screen",
    ], h_look_at_screen, threshold=0.68))

    r.register(Intent("describe_screen", [
        "describe my screen", "describe the screen", "describe this page",
        "describe what you see",
    ], h_describe_screen, threshold=0.72))

    r.register(Intent("read_screen", [
        "read the screen", "read what's on my screen", "read this for me",
        "read this text", "read out the text on screen",
    ], h_read_screen, threshold=0.70))

    r.register(Intent("screenshot", [
        "take a screenshot", "screenshot", "capture the screen",
        "grab a screenshot", "snap a screenshot",
    ], h_screenshot, threshold=0.74))

    r.register(Intent("click_element", [
        "click the send button", "click on the play button", "tap the github link",
        "click the search bar", "click on chrome icon",
        "press the start button", "press on muhammad abbas profile",
        "click on the link", "click the close x",
    ], h_click_element, threshold=0.62, negatives=[
        "click the center", "click middle of the screen",
        "ctrl c", "press enter", "press escape",
    ]))

    r.register(Intent("click_center", [
        "click the center of the screen", "click in the middle",
        "click center", "click middle",
    ], h_click_center, threshold=0.74))

    r.register(Intent("get_time", [
        "what time is it", "tell me the time", "what's the time",
        "current time", "time please", "what is the time",
    ], h_get_time, threshold=0.82, negatives=[   # V17.2: 0.74 → 0.82
        "what time is it in tokyo",
        "how are you doing",
        "how are you",
        "what are you doing",
        "what's up",
        "how is it going",
    ]))

    r.register(Intent("get_date", [
        "what's the date", "today's date", "what day is it",
        "what's today", "tell me the date", "what is today's date",
    ], h_get_date, threshold=0.82, negatives=[   # V17.1: was 0.74, too loose
        "what are you doing right now",
        "what will be the day on 29th of may",
        "what day is the 29th",
        "what is the day on march 5",
        "how many days until",
    ]))

    r.register(Intent("time_in_place", [
        "what time is it in tokyo", "time in london", "what's the time in pakistan",
        "current time in germany", "tell me the time in new york",
        "what time is it in pakistan and germany",
    ], h_time_in_place, threshold=0.70))

    r.register(Intent("get_weather", [
        "what's the weather in london", "weather in tokyo", "temperature in islamabad",
        "how's the weather in paris", "tell me the weather in karachi",
        "what's the temperature in berlin",
        "weather in london and pakistan",
        "temperature in tokyo, london and new york",
    ], h_get_weather, threshold=0.66))

    r.register(Intent("search_youtube", [
        "search mrbeast on youtube", "find a tutorial on youtube",
        "look up music on youtube", "search youtube for cats",
        "play despacito on youtube",
    ], h_search_youtube, threshold=0.74, negatives=[
        # bare "open/go to youtube" is focus_app, NOT search_youtube
        "open youtube", "go to youtube", "switch to youtube",
        "bring up youtube", "take me to youtube", "youtube",
    ]))

    r.register(Intent("search_google", [
        "google how to cook pasta", "search how to do this on google",
        "google the latest news", "look up python tutorials on google",
    ], h_search_google, threshold=0.70))

    return r
