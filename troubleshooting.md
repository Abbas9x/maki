# Maki Troubleshooting Guide

---

## Maki says "I couldn't find a microphone"

**Cause:** Windows is blocking microphone access, or no mic is detected.

**Fix:**
1. Press **Win + I** → Privacy & Security → Microphone
2. Turn on "Microphone access" and "Let apps access your microphone"
3. Make sure your microphone is plugged in and selected as default in Sound settings
4. Restart Maki

---

## pyaudio fails to install

`pip install pyaudio` often fails on Windows because it needs a C compiler.

**Fix — use a pre-built wheel:**

```powershell
pip install pipwin
pipwin install pyaudio
```

Or download the `.whl` directly from:
https://www.lfd.uci.edu/~gohlke/pythonlibs/#pyaudio

Choose the one matching your Python version (e.g. `PyAudio-0.2.13-cp311-cp311-win_amd64.whl` for Python 3.11 64-bit).

Then install it:
```powershell
pip install PyAudio-0.2.13-cp311-cp311-win_amd64.whl
```

---

## "I didn't catch that" — Maki doesn't understand speech

**Causes and fixes:**

1. **Background noise is too high** — Edit `config.py`, raise `MIC_ENERGY_THRESHOLD` to 400–600
2. **Speaking too quietly** — Speak louder and closer to the mic
3. **No internet** — Google Speech Recognition needs internet. Check your connection.
4. **Wrong microphone selected** — Open Windows Sound Settings → Input → choose your mic

---

## Maki starts but then freezes

**Possible cause:** TTS engine conflict.

**Fix:**
```powershell
pip install --upgrade pyttsx3
```

If it still freezes, try changing `TTS_VOICE_INDEX` in `config.py` from `0` to `1`.

---

## Ollama is not being detected

1. Make sure Ollama is running: open a terminal and run `ollama serve`
2. Visit http://localhost:11434 — you should see "Ollama is running"
3. Make sure you pulled the model: `ollama pull qwen2.5:7b`
4. Restart Maki

Maki will say which mode it's in at startup. Check the GUI top-right corner.

---

## Ollama model not found

```
Ollama running but model 'qwen2.5:7b' not found
```

Run:
```powershell
ollama pull qwen2.5:7b
```

Or change `OLLAMA_MODEL` in your `.env` file to a model you have installed:
```
ollama list
```

---

## App won't open (e.g. Discord)

Maki couldn't find the app at the expected path.

**Fix:** Open `config.py`, find `APP_PATHS`, and add the correct path:

```python
"discord": [
    r"C:\Users\<you>\AppData\Local\Discord\app-1.0.9030\Discord.exe",
],
```

To find the exact path: right-click the app's desktop shortcut → Properties → Target.

---

## "Permission denied" when opening an app

Run PowerShell as Administrator, then run Maki from there.

---

## The window opens but there's no sound

1. Check your speaker/headphone volume
2. Check Windows default audio output device
3. Try changing `TTS_VOICE_INDEX` in `config.py` to `1` (use a different voice)
4. Run: `python -c "import pyttsx3; e=pyttsx3.init(); e.say('test'); e.runAndWait()"`

---

## Google Speech fails / "RequestError"

This happens when:
- You're offline
- Google's API is temporarily unavailable

Maki will tell you it didn't understand. Just try again or check your connection.
