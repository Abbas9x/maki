# Maki — Personal AI Assistant: Full Project History & Guide

> A voice-driven personal AI assistant living on a Windows 11 PC.
> Location: `C:\Users\<you>\projectmaki`
> Python 3.14 · Gemini + Ollama + Python tools · CustomTkinter UI

This document captures the entire build journey (V4 → V8), the architecture,
every file, what each version fixed, how to test it, and known limitations.

---

## 1. What Maki Is

Maki is an always-on conversational assistant — closer to ChatGPT Voice / Jarvis
than a command bot. It:

- Listens continuously on every microphone, or on push-to-talk
- Understands natural speech, fuzzy/misheard names, and follow-up context
- Runs **Python tools** for instant local actions (time, weather, apps, screenshots, windows, storage)
- Uses **Gemini 2.5 Flash** for real reasoning / conversation
- Uses **Ollama qwen3:8b** as a hard-capped local fallback
- Uses **free web APIs** (Open-Meteo, DuckDuckGo, Wikipedia, optional Tavily/Brave) to answer live questions *inside the app* instead of dumping you into a browser
- Speaks back with Edge TTS (neural voice), pyttsx3 fallback
- Auto-starts on Windows login and greets you

---

## 2. Architecture & Routing

Per utterance, the brain routes in this priority order:

```
1. Pending confirmation (yes/no for risky action)
2. Pending clarification ("which app?" → "discord")
3. Pending web-search confirmation ("want me to search?" → "yeah sure")
4. Correction handler ("you didn't open it", "answer yourself")
5. Safety check (risky keywords → confirm)
6. Compound parser ("open discord and maximize it")
7. Fast rule-based classifier — Python tools, no AI cost
8. AI classify (Gemini → hard-capped Ollama → conversation)
9. Tool execution / conversation / clarification
10. Memory store
```

**Provider strategy:**

| Layer | Handles |
|---|---|
| **Python tools** | time, date, world time, weather, disk, folder/game size, app open/close, window control, screenshots, running apps |
| **app_index (fuzzy)** | resolves misheard app names → canonical app |
| **Gemini 2.5 Flash** | complex reasoning, explanations, coding help, general knowledge |
| **web_tools (live_lookup)** | Tavily → Brave → DuckDuckGo → Wikipedia — answers in-app |
| **Ollama qwen3:8b** | casual chat fallback ONLY — hard 5.5s ceiling, never the default |
| **Browser** | last resort only — when live lookup fails or user explicitly asks |

**Speed guarantees:** Gemini timeout 5s · Ollama hard ceiling 5.5s (abandoned if slow) ·
Gemini 429 → 10-minute cooldown (never retried during cooldown) · fast Python path
always tried before any AI call.

---

## 3. Files

### Core
| File | Purpose |
|---|---|
| `main.py` | App entry, GUI wiring, listen/processor loops, TTS, wake/unlock greeter |
| `brain.py` | Decision engine — routing, classifiers, tool executor, AI dispatch |
| `config.py` | All config: API keys (from `.env`), app paths, URLs, timeouts, flags |
| `memory.py` | Short-term memory: history, last action, screenshot/weather/web-search context |
| `safety.py` | Risky-action detection (delete, send, buy, etc.) |
| `gui.py` | CustomTkinter modern UI — chat bubbles, status orb, badges |

### Tools
| File | Purpose |
|---|---|
| `tools.py` | Ground-truth Python tools: time, date, disk, folder size, **game size**, **largest folders**, process check, running apps |
| `actions.py` | App open/close, Discord multi-strategy launch, PowerShell, folders, web, sleep PC |
| `window_tools.py` | Window enumerate / minimize / maximize / restore / focus; running-app detection (incl. Claude, browser tabs) |
| `app_index.py` | **V8** — adaptive fuzzy app resolver (Start Menu + processes + windows + phonetic aliases) |
| `weather_tools.py` | Live weather via Open-Meteo (no API key) |
| `world_time_tools.py` | World time — 50+ cities, country aliases, multi-TZ ambiguity, geocoding fallback |
| `web_tools.py` | Live web answers: Tavily → Brave → DuckDuckGo → Wikipedia |
| `screenshot_tools.py` | Screenshot / snip / clipboard-image / copy-last / open folder |
| `voice.py` | TTS — Edge TTS (AriaNeural) primary, pyttsx3 fallback |
| `speech.py` | Continuous multi-mic VAD listener, anti-echo, dedup, PTT |
| `wake_unlock_watcher.py` | Detects unlock / wake-from-sleep → greets you |

### Startup / scripts
| File | Purpose |
|---|---|
| `start_maki_hidden.vbs` | Silent launcher (no console), singleton guard, full logging, venv-aware |
| `start_maki.bat` | Visible launcher with logging |
| `run_maki_debug.bat` | Debug launcher — visible terminal + logs |
| `test_startup_launch.bat` | Runs the exact VBS Windows uses at login + tails both logs |
| `install_startup_task.ps1` | Installs auto-start (Startup-folder shortcut + Task Scheduler) |
| `uninstall_startup_task.ps1` | Removes both auto-start methods |
| `create_startup_shortcut.py` | Python fallback for creating the Startup-folder shortcut |
| `setup_autostart.md` | Auto-start setup + troubleshooting guide |

### Logs
| File | Purpose |
|---|---|
| `logs/launcher.log` | Written by the VBS — `VBS_STARTED`, python path, `RUN_COMMAND`, `VBS_DONE` |
| `logs/startup.log` | Written by `main.py` — `MAIN_STARTED`, calibration, `LISTEN_START`, `TTS_START/END`, all routing |

---

## 4. Version History

### V4 — Smart Brain + Gemini Integration
- Added Gemini 2.5 Flash as primary AI brain with graceful fallback
- Provider priority chain: Gemini → Ollama → Basic
- `psutil` process checking ("is Chrome running?")
- Acknowledgement intent ("okay" → "Got it.")
- Bug fixes: wake words, "what is my name", relative time, multi-timezone clarification

### V4.1 / V4.2 — Routing & Wake-Word Fixes
- Fixed time/apps regex failures, "okay and [command]" filler-prefix stripping
- **Disabled the strict wake gate** that was silently dropping all speech
- Added pipeline logging (`Transcript captured`, `Processing`, `Routed to`, `Response generated`)
- qwen3:8b fallback warning when not installed
- Discord open verification (path → shell → wait → process check)

### V5 — Jarvis Personality + Auto-Start
- Rewrote chat system prompt for natural, warm, non-robotic voice
- Narrowed web-search routing — general knowledge answered directly, not sent to browser
- Fixed follow-up context ("close" → "discord" now works)
- `_FILLER_PREFIX_RE`, "what model for time" explanation
- Gemini timeout, Discord path detection (`Update.exe --processStart`)
- Created startup scripts + Task Scheduler install

### V6 — Smart Router + Web Tools + Modern UI
- Smart router: casual chat → Ollama, complex reasoning → Gemini
- `weather_tools.py` — Open-Meteo live weather (no key)
- Tighter timeouts (Gemini 5s, Ollama 5s)
- Auto-start: Startup-folder shortcut + unlock trigger + VBS singleton guard
- GUI state system (Listening / Thinking / Speaking / etc.)

### V7 — Real In-App Web, World Time, Window Control
- **CustomTkinter UI overhaul** — chat bubbles, animated orb, badges
- `web_tools.py` rewrite — DuckDuckGo Instant Answer + Wikipedia summaries answered in-app
- `world_time_tools.py` — 50+ cities, country aliases, multi-TZ ambiguity, geocoding fallback
- `window_tools.py` — minimize/maximize/restore/focus + better app detection (Claude, browser tabs)
- `wake_unlock_watcher.py` — greets on unlock / wake from sleep
- Auto-start hardened (belt-and-suspenders), `tzdata` installed for Windows timezones

### V7.5 — Free Tool Boost + Screenshots
- `screenshot_tools.py` — take / clipboard / snip / save / copy-last / open folder
- `web_tools.py` — Tavily + Brave search added to the live-lookup chain (optional, key-gated)
- Auto-start diagnosis: separate `launcher.log`, `test_startup_launch.bat`, `create_startup_shortcut.py`
- Folder size tool, PowerShell aliases

### V7.5b — Log-Driven Routing Fixes
- Model-status phrases ("models are you using") → instant report, no AI
- Knowledge routing: "barack obama", "best AI model" → Gemini/live_lookup, **never Ollama**
- Compound parser ("open discord and maximize it", "close spotify and close chrome")
- Pending web-search confirmation ("yeah sure" actually runs the search)
- Correction handler ("you didn't open anything" → retries)
- PowerShell-misheard handling
- **Ollama hard 5.5s timeout** via futures — abandoned if slow (no more 14s hangs)
- Gemini 429 → 10-min cooldown, logged once/minute

### V8 — Intelligent Speech + Fuzzy Apps + Storage
- **Anti-echo** — ignores transcripts matching Maki's own last reply
- **Normalized dedup** — catches two-mic near-duplicates via similarity
- **Low-confidence filter** — drops single garbage tokens
- New logs: `LISTEN_START/STOP`, `TTS_START/END`, `SELF_ECHO_IGNORED`, `DUPLICATE_TRANSCRIPT_IGNORED`, `LOW_CONFIDENCE_TRANSCRIPT`
- **`app_index.py`** — adaptive fuzzy app resolver with phonetic aliases (`clawed`→claude, `dis chord`→discord)
- Fuzzy matching wired into open + close (high conf acts, medium conf asks)
- Clarification target-keep fix ("yes" no longer overwrites the resolved app)
- PowerShell admin mode (`Start-Process -Verb RunAs`, requires confirmation)
- **Storage tools**: `get_game_size` (League = 34.3 GB verified), `get_largest_folders`

### Critical mid-stream fix — Microphone selection
The always-on listener was locking onto a **dead microphone** at startup (a 1.2s
calibration window couldn't tell which mic you'd use). Fixed by making the listener
**monitor every input device simultaneously** — whichever mic actually receives
speech transcribes it; dead/silent mics never trigger. Each mic self-calibrates its
own VAD threshold from its own noise floor.

---

## 5. How to Test Everything

### A. Start / restart Maki

```powershell
# From C:\Users\<you>\projectmaki

# Debug mode (visible terminal + logs) — best for testing:
run_maki_debug.bat

# OR silent launch (same as auto-start):
& "$env:SystemRoot\System32\wscript.exe" "C:\Users\<you>\projectmaki\start_maki_hidden.vbs"

# Confirm it's running:
tasklist | findstr pythonw
```

### B. Auto-start setup & test

```powershell
cd C:\Users\<you>\projectmaki

# Remove any old task, install fresh:
powershell -ExecutionPolicy Bypass -File .\uninstall_startup_task.ps1
powershell -ExecutionPolicy Bypass -File .\install_startup_task.ps1

# Test the login launch WITHOUT rebooting (shows both logs):
test_startup_launch.bat

# Verify:
Get-ScheduledTask -TaskName MakiAutoStart
Get-Item "$([Environment]::GetFolderPath('Startup'))\Maki.lnk"
```

Look for `VBS_STARTED`, `RUN_COMMAND`, `VBS_DONE` in `logs\launcher.log` and
`MAIN_STARTED` in `logs\startup.log`. Then **reboot** to confirm Maki opens and greets you.

### C. Things to SAY (or type) to test each feature

#### Voice / listening
- Just talk normally — say **"hey maki, what time is it"**
- Watch `logs\startup.log` for `Heard [...]:` lines
- Say something right after Maki finishes speaking → should NOT echo (look for `SELF_ECHO_IGNORED`)
- Press-and-hold the **Hold to Talk** button as a fallback

#### Fast Python tools (instant, no AI)
| Say | Expect |
|---|---|
| "what time is it" | instant local time |
| "what time will it be in 2 hours" | instant relative time |
| "what time is it in England" | London time |
| "what time is it in Tokyo" | Tokyo time |
| "what time is it in Canada" | asks which city (multi-TZ) |
| "what's today's date" | today's date |
| "how much storage is left" | C: drive free space |

#### Weather (Open-Meteo, answered in-app)
- "what's the weather in Houston"
- "what's the temperature in London"
- "is it raining in Karachi"
- "what's the weather" → asks which city
- After a weather answer: **"convert that to celsius"** → instant Python conversion

#### Apps — open / close / fuzzy
| Say | Expect |
|---|---|
| "open discord" | opens + verifies Discord |
| "open chrome" / "open google" | opens Chrome |
| "open clawed" | fuzzy → opens Claude |
| "open dis chord" | fuzzy → opens Discord |
| "open power shell" | opens PowerShell |
| "open powershell as admin" | confirms, then elevated PowerShell |
| "close spotify" | closes Spotify |
| "close league" | fuzzy → League of Legends |
| "close gmail" | minimizes the Gmail browser window (explains tab limitation) |

#### Window control
- "minimize chrome"
- "maximize spotify"
- "maximize code" → fuzzy → VS Code
- "focus discord" / "switch to claude"
- "restore vs code"

#### Compound commands
- "open discord and maximize it"
- "close spotify and close google chrome"
- "open chrome and youtube"

#### Running apps / processes
- "what apps are running"
- "what apps are running in the background"
- "how many apps are running"
- "which apps or processes are running"
- "is Claude running"

#### Screenshots
- "take a screenshot" → saves to `Pictures\MakiScreenshots`
- "take a screenshot and copy it"
- "open snipping tool"
- "snip this area"
- "save this snip" (after using Win+Shift+S)
- "copy the last screenshot"
- "where are my screenshots"

#### Storage / size inspection
- "projectmaki folder size"
- "how much space is league taking" → e.g. "League of Legends is using about 34 GB"
- "how big is steam"
- "what are the largest folders on my pc"
- "show biggest games on my pc"

#### Knowledge / conversation (Gemini, or live_lookup if Gemini cooled down)
- "barack obama" → summary, not a browser redirect
- "what is the best AI model" → cautious direct answer
- "explain how neural networks work"
- "tell me about Pakistan"
- "who is Einstein"

#### Live / current info
- "what is the latest AI news" → offers a live search
- Then say **"yeah sure"** → actually runs the search and answers
- "current price of bitcoin" → live search

#### Conversational / emotional
- "how are you doing"
- "what are you doing"
- "I'm tired" → warm, empathetic reply
- "no that's fine" → "Alright."
- "okay" → "Got it."

#### Corrections / follow-up context
- After Maki opens something: "you didn't open it" → retries + explains
- After a browser redirect: "answer yourself" → forces a direct answer
- "close" → then "discord" → closes Discord (clarification follow-up)

#### Model / provider status
- "what model are you using" → Gemini + Ollama + Python tools
- "models are you using" → same
- "what model do you use for time" → explains Python handles time

#### Wake / unlock greeting
- Lock Windows (Win+L), wait, unlock → Maki greets "Welcome back, <your name>."
- Let the PC sleep, wake it → greeting after resume

### D. Watch the logs while testing

```powershell
# Live tail the main log:
Get-Content C:\Users\<you>\projectmaki\logs\startup.log -Wait -Tail 20
```

Key log markers: `Heard [...]`, `Routed to: ...`, `Processing time: N ms`,
`SELF_ECHO_IGNORED`, `DUPLICATE_TRANSCRIPT_IGNORED`, `TTS_START` / `TTS_END`,
`Gemini skipped: cooldown active`, `Ollama skipped/timeout`.

---

## 6. Configuration (`config.py` + `.env`)

API keys go in a `.env` file (never hardcoded, never printed):

```
GEMINI_API_KEY=your_key_here
GEMINI_MODEL=gemini-2.5-flash
OLLAMA_MODEL=qwen3:8b
# Optional — live web search upgrades:
TAVILY_API_KEY=
TAVILY_ENABLED=true
BRAVE_SEARCH_API_KEY=
BRAVE_SEARCH_ENABLED=false
```

Key `config.py` flags:
- `GEMINI_TIMEOUT_SECONDS = 5`, `OLLAMA_TIMEOUT_SECONDS = 5`
- `USE_SMART_ROUTER = True`, `PREFER_OLLAMA_FOR_CASUAL_CHAT = True`
- `WAKE_WORD_STRICT = False` (process all speech)
- `DISCORD_PATH = ""` (auto-detect)

---

## 7. Dependencies

```
pip install -r requirements.txt
```

Includes: `google-genai`, `SpeechRecognition`, `sounddevice`, `numpy`, `psutil`,
`edge-tts`, `pyttsx3`, `python-dotenv`, `requests`, `customtkinter`, `pygetwindow`,
`pywin32`, `tzdata`, `pyautogui`, `pillow`.

Plus **Ollama** running locally with `qwen3:8b` pulled (`ollama pull qwen3:8b`).

---

## 8. Known Limitations (honest)

1. **UI** — functional CustomTkinter (chat bubbles, orb, badges) but not heavily
   polished; no further visual work done in V8.
2. **Anti-echo is text-based** — backs up the mic-mute-during-TTS; if STT badly
   garbles an echo it could slip through (rare).
3. **`get_game_size`** works for standard Steam/Epic/Riot install paths; non-standard
   install locations need a path hint.
4. **Wake-from-sleep** detection uses a wall-clock-gap heuristic (>90s) — Windows
   doesn't fire a clean user-space resume event in every config.
5. **Fuzzy close** only auto-acts at ≥0.90 confidence (closing the wrong app is
   costly); medium confidence always asks first.
6. **Gemini free tier** is rate-limited — during a 429 cooldown, knowledge questions
   fall to live_lookup (Wikipedia/DDG) or a short honest fallback.
7. **Streaming responses** not implemented — long Gemini replies arrive all at once.

---

## 9. Troubleshooting Quick Reference

| Symptom | Check |
|---|---|
| Maki doesn't start on login | `logs\launcher.log` — is `VBS_STARTED` there? Is `Maki.lnk` in `shell:startup`? |
| Maki starts but no GUI | `logs\startup.log` — is `MAIN_STARTED` there? Python crash after? |
| Voice not heard | `logs\startup.log` — which mic SELECTED? Any `Heard [...]` lines? Windows mic privacy? |
| Double responses | Should be fixed — look for `DUPLICATE_TRANSCRIPT_IGNORED` / `SELF_ECHO_IGNORED` |
| Slow responses | `Gemini skipped: cooldown active` or `Ollama skipped/timeout` — both are capped now |
| Wrong app opens | fuzzy match — say the fuller name, or check `app_index` aliases |

Full guide: `setup_autostart.md` and `troubleshooting.md`.

---

*Generated as the consolidated project record. Maki V8.*

---

## 10. V9 → V18 Evolution (added 2026-05-20)

### V9 – V13 (interim)
- Hardened anti-echo, multi-mic VAD, fuzzy app close confirmation, compound
  command parser polish, Tavily/Brave search wired with daily caps,
  screenshot pipeline, window-focus + drag handlers.
- TTS upgraded to persistent asyncio loop (no per-call loop overhead) with
  Edge `AriaNeural` voice; pyttsx3 retained as fallback.

### V14 – V14.5 (perception + intent routing)
**Problem:** Maki routed too many utterances to the slow conversational agent,
misunderstood mishearings, and depended on a growing pile of hardcoded regex.

**Fixes shipped:**
- **Perception layer** — a fast Cerebras `gpt-oss-120b` call that rewrites the
  raw STT transcript using recent context (resolves pronouns, fixes mishearings,
  expands "weather for them" → "weather in Toronto").
- **Semantic intent router** — 30 intents with example utterances embedded via
  `BGE-small-en-v1.5` (ONNX) → ~16 ms cosine-similarity routing replaces dozens
  of regex.
- **Cerebras as primary brain** — `gpt-oss-120b` (1 M tok/day free, ~600 ms,
  2 700 tok/s, native tool calling). Gemini 2.5 Flash kept as secondary +
  vision (1500 req/day).
- **Tool-call hygiene** — `_strip_tool_call_junk()` prevents raw JSON tool
  envelopes from being spoken; tool-free system prompt added.
- **Title cleaning** — `_clean_title()` strips noisy "- Google Chrome -
  Cerebras Cloud" suffixes; `_distill_app_name()` extracts known apps from
  noise ("brain chrome" → "chrome").
- **Pronoun guard** — `_PRONOUN_TARGETS_RE` rejects unresolved pronouns at the
  handler boundary.

### V15 – V17 (vision + stability)
- `qwen3-vl:2b` local vision (~5–9 s on GPU, ~2.8 GB VRAM).
- Switched screenshot capture from `PIL.ImageGrab(all_screens=True)` to `mss`
  with explicit monitor index + `finally` cleanup (`sct.close()`).
- Vision cache lock against concurrent calls.
- Fixed multi-city weather, scroll patterns, multi-key chords, vision retry,
  `focus_window` matching.
- 442 / 442 tests passing across 12 suites at end of V17.

### V18 (Hermes 3 + Think toggle + Stop + Barge-in)  ← this session
User mandate verbatim: *"THE PERFECT VERSION OF MAKIS FUNCTIONALITIES SHOULD
ALL BE THERE WITH NO COMPROMISE ON IT BEING DUMB OR SLOW."* Voice (AriaNeural)
must remain unchanged.

**Phase 1 — Local-model swap:**
- `.env`: `OLLAMA_MODEL=qwen3:8b` → `hermes3:8b` (91 % function-call accuracy).
- `vision_tools.py` fallback updated to `hermes3:8b`.
- `brain.check_ollama()` confirmed selecting `hermes3:8b`.

**Phase 2 — Think-mode toggle (UI + voice):**
- `memory.py` got `set_think_mode / is_think_mode` + thread lock.
- `gui.py`: `_think_btn` (🧠 Think, violet on hover) + `set_think()` external
  setter + `on_think_toggle` wiring.
- `brain._handle_voice_meta()` recognises sticky on / off / one-shot triggers
  (`"keep thinking"`, `"smart mode on"`, `"stay smart"`, `"stop thinking"`,
  `"smart mode off"`, `"go fast"`). Replies contain `"on"` or `"fast"` for
  test assertions.
- When think-mode is on, perception runs **first** in `process()` step 3.2 so
  Cerebras gets the cleaned utterance every turn.

**Phase 3 — Stop button + voice phrases:**
- `memory.py`: `request_stop / consume_stop / is_stop_pending` (one-shot flag).
- `gui.py`: `_stop_btn` 🛑 with `on_stop` wiring.
- `brain._handle_voice_meta()` matches `stop / shut up / be quiet / quiet /
  cancel / nevermind`.
- `voice.stop()` (new public API):
  - sets `_stop_evt`,
  - drains the TTS queue (each pending `done_evt.set()`),
  - schedules `_stop_evt.clear()` 200 ms later so the next utterance plays.
- `voice._play_mp3()` rewritten to poll MCI status every 50 ms and exit on
  `_stop_evt` — replaces blocking `wait` so stop is instant.
- `main._reply()` starts with `if memory.consume_stop(): return` to suppress
  any TTS that was queued before the stop fired.

**Phase 4 — Barge-in:**
- `speech._AlwaysOnListener.signal_user_interrupt()` added.
- `_stream()` (the per-mic listener) no longer fully drains while `_muted`
  during TTS. Instead it tracks `_barge_cnt`; once RMS ≥ `barge_thr` (=`thr*2`)
  for **8 consecutive chunks (~250 ms)** it calls `signal_user_interrupt()`,
  which triggers `voice.stop()` and re-opens STT. This avoids false positives
  from clicks / chair noise.

**Phase 5 — Tests (`test_v18.py`):**
35 / 35 pass. Covers:
1. Hermes 3 swap (config + Ollama tags + `brain._ollama_model_actual`).
2. Think-mode default OFF, setter flips, all 6 voice phrases flip state
   correctly, real commands (`open chrome`, `scroll down`, …) do **not**
   trigger the toggle.
3. Six stop phrases set the flag; `consume_stop` clears once; `voice.stop`
   exists.
4. `signal_user_interrupt` exists on `AlwaysOn`; `_stream` source contains
   `barge_thr`, `signal_user_interrupt`, and the `_barge_cnt >= 8` gate.
5. GUI has `_think_btn`, `_stop_btn`, `on_think_toggle`, `on_stop`,
   `set_think`.
6. `agent / intents / perception / intent_router` still import cleanly.

### Known issue carried out of V18
- **Recurring SIGSEGV (exit 139)** has appeared intermittently from V14
  through V18. Suspected contributors (un-confirmed):
  1. New barge-in path keeps mic streams active while `_muted` (previously
     fully drained).
  2. `voice._play_mp3` now opens a fresh MCI alias per utterance
     (`maki_tts_<ms>`); a race with the poller's `close` could leak handles.
  3. Possible contention between `speech.py` capture and `voice.py` MCI under
     heavy back-to-back use.
- Crash log lives at
  `C:\Users\<you>\AppData\Local\Temp\claude\C--Users-<you>-projectmaki\…\tasks\<id>.output`.
- Phase 0 ("modular providers refactor") was intentionally deferred to avoid
  introducing risk alongside the V18 feature work.

### Files touched in V18
| File | Change |
|---|---|
| `.env` | `OLLAMA_MODEL=hermes3:8b` |
| `vision_tools.py` | Fallback model → `hermes3:8b` |
| `memory.py` | `set/is_think_mode`, `request/consume/is_stop_pending` |
| `gui.py` | `_think_btn`, `_stop_btn`, `set_think`, `on_think_toggle`, `on_stop` |
| `brain.py` | `_handle_voice_meta`, perception-first when think-mode on |
| `voice.py` | `_stop_evt`, polled `_play_mp3`, public `stop()` |
| `speech.py` | `signal_user_interrupt`, sustained-RMS barge-in (8 chunks) |
| `main.py` | wires think/stop buttons; `_reply` consumes stop flag |
| `test_v18.py` | new 35-check suite |

*V18 record. Updated 2026-05-20.*
