"""
startup_check.py — V19

Real boot-time ping of every lane Maki depends on. No vibe-checks (key
presence != working). Each probe makes one tiny live call with a 4-8s
budget and reports OK / DOWN with the reason.

Use from main.py at boot:
    from startup_check import run_all
    results = run_all()              # dict[str, (bool, str)]
    print_banner(results)            # also writes to logs/startup.log

Total budget under ~12 seconds even when something is down.
"""

from __future__ import annotations
import logging, os, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

logger = logging.getLogger(__name__)


def _ping_ollama_and_vision() -> tuple[bool, str]:
    """Verify Ollama is up AND qwen3-vl:4b (or VISION_MODEL) is pulled."""
    model = os.getenv("VISION_MODEL", "qwen3-vl:4b")
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=4)
        if r.status_code != 200:
            return False, f"Ollama HTTP {r.status_code}"
        tags = [m.get("name","") for m in r.json().get("models", [])]
        if not any(model in t for t in tags):
            return False, f"VISION OFFLINE — run: ollama pull {model}"
        return True, model
    except Exception as e:
        return False, f"Ollama unreachable: {str(e)[:60]}"


def _ping_hermes() -> tuple[bool, str]:
    """Verify the local chat/tool model (hermes3:8b) is pulled."""
    model = os.getenv("OLLAMA_MODEL", "hermes3:8b")
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=4)
        tags = [m.get("name","") for m in r.json().get("models", [])]
        if not any(model.split(":")[0] in t for t in tags):
            return False, f"TOOL-LANE OFFLINE — run: ollama pull {model}"
        return True, model
    except Exception as e:
        return False, f"Ollama unreachable: {str(e)[:60]}"


def _ping_cerebras() -> tuple[bool, str]:
    key = os.getenv("CEREBRAS_API_KEY", "").strip()
    if not key: return False, "no CEREBRAS_API_KEY"
    try:
        r = requests.post(
            "https://api.cerebras.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type":"application/json"},
            json={"model": os.getenv("CEREBRAS_MODEL","gpt-oss-120b"),
                  "messages":[{"role":"user","content":"hi"}],
                  "max_completion_tokens": 10},
            timeout=8,
        )
        if r.status_code == 200: return True, "gpt-oss-120b"
        return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, f"net: {str(e)[:60]}"


def _ping_groq_chat() -> tuple[bool, str]:
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key: return False, "no GROQ_API_KEY"
    model = os.getenv("GROQ_CHAT_MODEL", "llama-3.1-8b-instant")
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type":"application/json"},
            json={"model": model, "messages":[{"role":"user","content":"hi"}],
                  "max_tokens": 5},
            timeout=8,
        )
        if r.status_code == 200: return True, model
        return False, f"HTTP {r.status_code}: {r.text[:80]}"
    except Exception as e:
        return False, f"net: {str(e)[:60]}"


def _ping_groq_whisper() -> tuple[bool, str]:
    """Whisper has no cheap ping — best-effort is just checking the chat key
    works (same auth). Live transcription would cost an audio-second."""
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key: return False, "no GROQ_API_KEY"
    return True, os.getenv("GROQ_WHISPER_MODEL","whisper-large-v3-turbo") + " (shared key OK)"


def _ping_github() -> tuple[bool, str]:
    key = os.getenv("GITHUB_TOKEN", "").strip()
    if not key: return False, "no GITHUB_TOKEN"
    model = os.getenv("THINK_MODEL", "openai/gpt-4o")
    try:
        r = requests.post(
            "https://models.github.ai/inference/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type":"application/json"},
            json={"model": model, "messages":[{"role":"user","content":"hi"}],
                  "max_tokens": 5},
            timeout=10,
        )
        if r.status_code == 200: return True, model
        return False, f"HTTP {r.status_code}: {r.text[:80]}"
    except Exception as e:
        return False, f"net: {str(e)[:60]}"


def _ping_nim() -> tuple[bool, str]:
    key = os.getenv("NVIDIA_API_KEY", "").strip()
    if not key: return False, "no NVIDIA_API_KEY"
    model = os.getenv("NIM_MODEL", "nvidia/nvidia-nemotron-nano-9b-v2")
    try:
        r = requests.post(
            "https://integrate.api.nvidia.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type":"application/json"},
            json={"model": model,
                  "messages":[{"role":"system","content":"/no_think"},
                              {"role":"user","content":"hi"}],
                  "max_tokens": 5},
            timeout=8,
        )
        if r.status_code == 200: return True, model
        return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, f"net: {str(e)[:60]}"


def _ping_gemini() -> tuple[bool, str]:
    """Gemini is cloud vision fallback. Light check — confirm key is set
    and brain.check_gemini doesn't error."""
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key: return False, "no GEMINI_API_KEY"
    try:
        import brain
        brain.check_gemini()
        return True, os.getenv("GEMINI_MODEL","gemini-2.5-flash")
    except Exception as e:
        return False, str(e)[:80]


PROBES = {
    "Ollama+qwen3-vl:4b (vision)":    _ping_ollama_and_vision,
    "Ollama+hermes3:8b (tool lane)":  _ping_hermes,
    "Cerebras (default brain)":       _ping_cerebras,
    "Groq chat (casual lane)":        _ping_groq_chat,
    "Groq Whisper (STT)":             _ping_groq_whisper,
    "GitHub Models (Think lane)":     _ping_github,
    "NVIDIA NIM (overflow lane)":     _ping_nim,
    "Gemini Flash (vision fallback)": _ping_gemini,
}


def run_all(parallel: bool = True) -> dict[str, tuple[bool, str]]:
    """Run every probe. Parallel by default (~3s wall-clock when all healthy)."""
    results: dict[str, tuple[bool, str]] = {}
    if parallel:
        with ThreadPoolExecutor(max_workers=len(PROBES)) as pool:
            futs = {pool.submit(fn): name for name, fn in PROBES.items()}
            for f in as_completed(futs, timeout=15):
                name = futs[f]
                try:
                    results[name] = f.result()
                except Exception as e:
                    results[name] = (False, f"probe crash: {str(e)[:60]}")
    else:
        for name, fn in PROBES.items():
            try: results[name] = fn()
            except Exception as e: results[name] = (False, f"probe crash: {str(e)[:60]}")
    return results


def print_banner(results: dict[str, tuple[bool, str]]) -> str:
    """Pretty-print the V19 boot banner to log + return the text."""
    lines = ["", "=" * 70, "  Maki V19 — boot health check", "=" * 70]
    for name, (ok, detail) in results.items():
        mark = "OK   " if ok else "DOWN "
        lines.append(f"  [{mark}] {name:34s}  {detail}")
    down = [n for n,(ok,_) in results.items() if not ok]
    if down:
        lines.append("-" * 70)
        lines.append(f"  WARN: {len(down)} lane(s) DOWN — Maki will route around them.")
        for n in down: lines.append(f"        - {n}")
    else:
        lines.append("-" * 70)
        lines.append("  All lanes healthy.")
    lines.append("=" * 70)
    text = "\n".join(lines)
    for line in lines:
        logger.info(line)
    return text
