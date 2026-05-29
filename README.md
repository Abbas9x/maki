# Maki — Personal PC AI Assistant

Maki is a local Windows voice assistant that listens for **"Hey Maki"**, understands natural speech,
and performs safe actions on your PC — opening apps, searching the web, and more.

---

## What Works Right Now (v1)

- ✅ GUI window with status display
- ✅ Startup greeting ("Welcome back, <your name>")
- ✅ Wake phrase detection ("Hey Maki")
- ✅ Natural voice responses using pyttsx3
- ✅ Microphone voice input
- ✅ Open apps: Discord, Chrome, Docker Desktop, VS Code
- ✅ Open websites: YouTube, Gmail, GitHub, n8n dashboard
- ✅ Search Google and YouTube by voice
- ✅ Tell current time and date (using Python — not AI)
- ✅ Open folders: Downloads, Documents, project folders
- ✅ Ollama mode for natural conversation (falls back to Basic Mode automatically)
- ✅ Safety blocks for risky actions

---

## What's Coming Later

See `future_features.md` for the full roadmap.

---

## Quick Start

### Step 1 — Prerequisites

- Python 3.10 or newer — https://www.python.org/downloads/
  - During install, check **"Add Python to PATH"**
- A working microphone
- Internet connection (for Google speech-to-text)

### Step 2 — Set Up the Project

Open PowerShell inside `C:\Users\<you>\projectmaki` and run:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

> If `pyaudio` fails to install, see **Troubleshooting** below.

> **Note on a fresh clone:** the virtual environment (`.venv/`), local model
> weights (`models/`), and runtime logs (`logs/`) are intentionally **not**
> in the repo (see `.gitignore`). `pip install -r requirements.txt` rebuilds
> the venv; the embedding model is fetched in the next step; logs are created
> on first run.

### Step 2.5 — Download the intent-embedding model (one-time, ~130 MB)

Maki's semantic intent router uses **BGE-small-en-v1.5** (ONNX, runs on CPU).
It is not bundled. Download it into `models/bge-small-en/` once:

```powershell
pip install huggingface_hub
python -c "from huggingface_hub import snapshot_download; snapshot_download('BAAI/bge-small-en-v1.5', local_dir='models/bge-small-en', allow_patterns=['onnx/model.onnx','tokenizer.json','config.json','tokenizer_config.json','vocab.txt','special_tokens_map.json'])"
```

After this, `models/bge-small-en/onnx/model.onnx` and
`models/bge-small-en/tokenizer.json` exist and the router loads at startup.
(If the model is missing, Maki automatically falls back to the Ollama
`nomic-embed-text` embedder — slower, but it still works.)

### Step 3 — Create Your .env File

```powershell
copy .env.example .env
```

You don't need to edit `.env` to get started — the defaults work.

### Step 4 — Run Maki

```powershell
python main.py
```

Maki will open a window and say **"Welcome back, <your name>."**

---

## How to Test the Wake Phrase

1. Run `python main.py`
2. Wait for the green status: **"Listening for 'Hey Maki'…"**
3. Say clearly: **"Hey Maki"**
4. Maki responds and waits for your command
5. Say: **"Open YouTube"** or **"What time is it?"**

---

## How to Use Ollama (Local AI Brain)

Ollama lets Maki understand natural speech more flexibly and runs the local
vision model `qwen3-vl:4b`.

### Install Ollama (required, one-time)

Download from: https://ollama.com/download

### Pull the models Maki needs (one-time)

```powershell
ollama pull hermes3:8b
ollama pull qwen3-vl:4b
```

This downloads ~8 GB total. Do it once.

### Starting Ollama — **automatic since V19**

You no longer need to run `ollama serve` in a separate terminal.

When you run `python main.py`, Maki automatically:
1. Checks if Ollama is already running on `http://localhost:11434`
2. If not, spawns `ollama serve` silently in the background (no popup window)
3. Waits up to 10 seconds for it to come up
4. Continues — if Ollama can't start, Maki falls back to cloud-only operation

Ollama keeps running when Maki closes (detached background process), so it
won't repeatedly cold-start.

### Test Ollama is Running

Open your browser and visit: http://localhost:11434

You should see: `Ollama is running`

### Change the Ollama Model

Edit `.env`:

```
OLLAMA_MODEL=llama3.2:3b
```

Then restart Maki. Available models: https://ollama.com/library

---

## How to Edit App Paths

Open `config.py` and find the `APP_PATHS` dictionary.

Example — add Spotify:

```python
"spotify": [
    r"C:\Users\<you>\AppData\Roaming\Spotify\Spotify.exe",
],
```

Then say "Hey Maki, open Spotify."

---

## How to Make Maki Start With Windows

See `setup_windows_startup.md` for step-by-step Task Scheduler instructions.

---

## Safety Notes

Maki will **never**:
- Delete files without your confirmation
- Send emails or messages
- Make purchases
- Run random AI-generated shell commands
- Execute anything outside its whitelist in `actions.py`

Risky commands are detected and blocked with a spoken warning.

---

## Folder Structure

```
projectmaki/
├── main.py          ← Start here
├── config.py        ← Your settings (name, paths, URLs)
├── brain.py         ← Intent classification (Basic + Ollama)
├── actions.py       ← What Maki is allowed to do
├── gui.py           ← The window
├── voice.py         ← Text-to-speech
├── speech.py        ← Microphone input
├── wake_word.py     ← "Hey Maki" detection
├── safety.py        ← Risky action blocking
├── requirements.txt ← Python packages
├── .env.example     ← Template for your .env file
└── .env             ← Your private settings (don't commit this)
```


## V19 routing — which utterance goes where

Six brain lanes, picked per utterance by lane_classifier.select_lane():

| Lane | Provider | Triggers |
|---|---|---|
| github_premium | GitHub Models (Claude Sonnet 4.5 by default) | Think toggle ON |
| hermes_tools | Hermes 3 local (Ollama) | Tool-call intents at conf >= 0.78 |
| groq_8b | Groq llama-3.1-8b-instant | Social / greetings / jokes |
| cerebras_120b | Cerebras gpt-oss-120b | Knowledge / explanations (default) |
| nim_nemotron | NVIDIA NIM Nemotron Nano 9B | Cerebras 8K guard OR Groq cap OR GitHub cap hit |
| vision | qwen3-vl:4b local + Gemini fallback | Screen perception |

Selection precedence: social keyword -> follow-up strong cue -> follow-up inheritance -> tool override -> vision intent -> think mode -> difficulty tag -> default.

STT: Groq Whisper Large v3 Turbo (~200ms) with faster-whisper as offline fallback. TTS: Edge AriaNeural (unchanged).

Routing decisions log to logs/v19_routing.jsonl. Token-budget decisions log to logs/v19_budget.jsonl. Per-call breadcrumbs in logs/v19_actions.jsonl.
