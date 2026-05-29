# Maki — Future Features Roadmap

This file tracks planned upgrades. Each item is a separate project — build one at a time.

---

## Voice & Speech

- [ ] **Better wake word engine** — Replace the basic Google STT wake detection with a local engine like `openWakeWord` or `Porcupine` so Maki doesn't need internet just to hear "Hey Maki"
- [ ] **Offline speech-to-text** — Use Vosk or Whisper (local) so Maki works without internet
- [ ] **ElevenLabs voice** — Replace pyttsx3 with a natural-sounding ElevenLabs voice via their API
- [ ] **Custom Maki voice character** — Clone a voice or choose an ElevenLabs preset that fits Maki's personality

---

## AI Brain

- [ ] **Conversation memory** — Maki remembers what you talked about in the same session
- [ ] **Claude API mode** — Use Claude as the smart brain for complex questions and planning
- [ ] **Gemini API mode** — Alternative cloud brain option
- [ ] **Local model switching** — Say "Hey Maki, switch to llama3" and it changes models live
- [ ] **Smarter intent parsing** — Multi-turn commands ("open YouTube and then search for…")

---

## Email & Calendar

- [ ] **Gmail integration** — Read unread emails by voice ("Maki, do I have any emails?")
- [ ] **Scholarship email scanner** — Scan inbox for scholarship/opportunity emails and summarize
- [ ] **Google Calendar** — "What's on my calendar today?" / "Add a meeting at 3pm"
- [ ] **Send email with confirmation** — Compose and send with full confirmation flow

---

## Files & Productivity

- [ ] **File search** — "Hey Maki, find my CV" searches Downloads and Documents
- [ ] **File summarization** — Read a document and give you a summary
- [ ] **Clipboard assistant** — "Hey Maki, what did I just copy?" reads clipboard content
- [ ] **Screenshot analysis** — Take a screenshot and describe what's on screen

---

## n8n Integration

- [ ] **Trigger n8n workflows** — "Hey Maki, run my daily digest workflow"
- [ ] **n8n status check** — "Is n8n running?" checks Docker container status
- [ ] **Workflow list** — List available n8n workflows by voice

---

## PC Control

- [ ] **Volume control** — "Turn volume up/down"
- [ ] **Brightness control** — "Make the screen brighter"
- [ ] **App switcher** — "Switch to Chrome"
- [ ] **Window management** — "Minimise everything" / "Snap Chrome to the left"
- [ ] **System info** — "How much RAM am I using?" / "What's my CPU at?"

---

## Web & Research

- [ ] **Real-time weather** — Pull weather from a free API like Open-Meteo
- [ ] **Wikipedia quick facts** — "Hey Maki, what is Docker?"
- [ ] **News headlines** — Read today's top headlines from a free RSS feed

---

## Multi-Modality

- [ ] **Screen reading** — Maki can read what's on screen and answer questions about it
- [ ] **Image understanding** — Show Maki an image and ask about it
- [ ] **Code assistant** — "Hey Maki, explain this error" while VS Code is open

---

## Infrastructure

- [ ] **Docker container status** — "Is my n8n container running?"
- [ ] **Port status checks** — "Is anything running on port 5678?"
- [ ] **Process monitor** — "How much memory is Python using?"

---

## Notes

- Build one feature at a time
- Keep Basic Mode always working as a fallback
- Every new action goes into `actions.py` — never run arbitrary AI-generated commands
- Every risky new action goes through `safety.py` confirmation flow
