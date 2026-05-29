"""
web_tools.py — V7: real in-app live web answers (no API key).

Strategy when user asks a factual / current question:
  1. DuckDuckGo Instant Answer API → fast factual snippets (free, no key)
  2. Wikipedia REST summary       → reliable encyclopedia answers (free, no key)
  3. Honest fallback              → tell the user, optionally open browser

These tools FETCH and RETURN text so Maki can speak the answer
inside the app instead of dumping the user into a browser tab.
"""

import datetime as _dt
import logging, os, re, urllib.parse, webbrowser
import requests

logger = logging.getLogger(__name__)

DDG_API   = "https://api.duckduckgo.com/"
WIKI_API  = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
WIKI_SRCH = "https://en.wikipedia.org/w/api.php"
TIMEOUT   = 5
UA        = {"User-Agent": "MakiAssistant/7.5 (personal use)"}

# ── Optional paid/keyed search providers ─────────────────────────────────────
TAVILY_API_KEY      = os.getenv("TAVILY_API_KEY", "").strip()
TAVILY_ENABLED      = os.getenv("TAVILY_ENABLED", "true").lower() != "false"
TAVILY_URL          = "https://api.tavily.com/search"

BRAVE_SEARCH_KEY    = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
BRAVE_ENABLED       = os.getenv("BRAVE_SEARCH_ENABLED", "false").lower() == "true"
BRAVE_URL           = "https://api.search.brave.com/res/v1/web/search"


def _shorten(text: str, limit: int = 360) -> str:
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    # cut at sentence boundary if possible
    last_dot = cut.rfind(".")
    return (cut[:last_dot + 1] if last_dot > limit * 0.6 else cut.rsplit(" ", 1)[0] + "…")


# V12: detect "current/latest/now" queries to ask Tavily for fresh news mode
_FRESH_RE = re.compile(
    r"\b(current(?:ly)?|latest|now|today|this\s+(?:week|month|year)|"
    r"recent(?:ly)?|trending|viral|breaking|news|"
    r"right\s+now|as\s+of|nowadays|these\s+days|2024|2025|2026)\b",
    re.I,
)

# V12: strip "2020/2021/2022/2023" — qwen3's training-era years that hijack
# searches into stale results. Keep current/future year tokens if present.
_CURRENT_YEAR  = _dt.date.today().year
_STALE_YEARS_RE = re.compile(r"\b(20[12][0-9])\b")


def _strip_stale_years(q: str) -> str:
    """Remove year tokens that are 2+ years old, so freshness-sensitive
    queries don't get hijacked by the model's training-era bias."""
    def _r(m):
        try:
            y = int(m.group(1))
        except Exception:
            return m.group(0)
        # Drop anything 2+ years stale; keep current and future years
        return "" if y < (_CURRENT_YEAR - 1) else m.group(0)
    out = _STALE_YEARS_RE.sub(_r, q)
    return re.sub(r"\s{2,}", " ", out).strip()


# ── Tavily (real-time AI-friendly search with answer + sources) ──────────────
def tavily_search(query: str) -> dict:
    """Tavily returns a synthesized answer plus top results. No key → {}."""
    if not TAVILY_ENABLED or not TAVILY_API_KEY or not query.strip():
        return {}
    # V12: strip stale-year tokens before hitting Tavily
    q = _strip_stale_years(query)
    fresh = bool(_FRESH_RE.search(q))
    try:
        payload = {
            "api_key":        TAVILY_API_KEY,
            "query":          q,
            "search_depth":   "advanced" if fresh else "basic",
            "include_answer": "advanced" if fresh else True,
            "max_results":    5 if fresh else 3,
        }
        if fresh:
            # 'topic=news' gives recent articles; 'days' caps lookback
            payload["topic"] = "news"
            payload["days"]  = 30
        r = requests.post(
            TAVILY_URL,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=TIMEOUT + 2,
        )
        if r.status_code != 200:
            logger.info("Tavily HTTP %s: %s", r.status_code, r.text[:120])
            return {}
        data = r.json()
    except Exception as e:
        logger.info("Tavily failed: %s", e)
        return {}

    answer = (data.get("answer") or "").strip()
    results = data.get("results") or []
    if not answer and results:
        # fall back to top snippet
        answer = (results[0].get("content") or "").strip()
    if not answer:
        return {}
    # build a short source line
    src_bits = []
    for r in results[:2]:
        title = (r.get("title") or "").strip()
        url   = r.get("url") or ""
        if title and url:
            src_bits.append(f"{title} ({_domain(url)})")
        elif url:
            src_bits.append(_domain(url))
    sources = "; ".join(src_bits)
    return {
        "answer":  _shorten(answer),
        "source":  sources or "Tavily",
        "kind":    "Tavily",
        "results": results,
    }


def _domain(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lstrip("www.")
    except Exception:
        return url


# ── Brave Search (basic; uses 'extra_snippets' for short text) ───────────────
def brave_search(query: str) -> dict:
    if not BRAVE_ENABLED or not BRAVE_SEARCH_KEY or not query.strip():
        return {}
    try:
        r = requests.get(
            BRAVE_URL,
            headers={
                "Accept":            "application/json",
                "X-Subscription-Token": BRAVE_SEARCH_KEY,
            },
            params={"q": query, "count": 3},
            timeout=TIMEOUT + 1,
        )
        if r.status_code != 200:
            logger.info("Brave HTTP %s", r.status_code)
            return {}
        data = r.json()
    except Exception as e:
        logger.info("Brave failed: %s", e)
        return {}
    web = (data.get("web") or {}).get("results") or []
    if not web:
        return {}
    top = web[0]
    text = (top.get("description") or "").strip()
    if not text:
        return {}
    return {
        "answer": _shorten(text),
        "source": f"{top.get('title','')} ({_domain(top.get('url',''))})".strip(),
        "kind":   "Brave",
    }


# ──────────────────────────────────────────────────────────────────────────────
# Live info classifier — only true time-sensitive questions go to web
# ──────────────────────────────────────────────────────────────────────────────

_LIVE_NOW_RE = re.compile(
    r"\b("
    r"right\s+now|today|currently|as\s+of\s+(?:now|today)|"
    r"latest|breaking|live\s+(?:score|news|price|standings?)|"
    r"(?:stock|share)\s+price|"
    r"who\s+(?:won|is\s+winning)|"
    r"score\s+of\s+the\s+\w+\s+game"
    r")\b",
    re.I,
)


def needs_live_data(text: str) -> bool:
    """True only if the question genuinely requires current/real-time data."""
    return bool(_LIVE_NOW_RE.search(text))


# ──────────────────────────────────────────────────────────────────────────────
# DuckDuckGo Instant Answer  (best for: definitions, calculator, factoids)
# ──────────────────────────────────────────────────────────────────────────────

def ddg_instant_answer(query: str) -> dict:
    """
    Returns {'answer': str, 'source': str} on hit, {} on miss.
    Hits AbstractText, Answer, or Definition fields.
    """
    if not query or not query.strip():
        return {}
    try:
        r = requests.get(
            DDG_API,
            params={
                "q":            query,
                "format":       "json",
                "no_html":      "1",
                "skip_disambig":"1",
                "no_redirect":  "1",
            },
            headers=UA, timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.info("DDG instant answer failed: %s", e)
        return {}

    for field, label in (("Answer", "DDG"), ("AbstractText", "Wikipedia"),
                         ("Definition", "Dictionary")):
        text = (data.get(field) or "").strip()
        if text and len(text) > 4:
            src = data.get(f"{field}URL") or data.get("AbstractURL") or ""
            return {"answer": text, "source": src or label, "kind": label}
    # RelatedTopics can be useful for ambiguous queries
    related = data.get("RelatedTopics") or []
    for rt in related[:1]:
        text = (rt.get("Text") or "").strip()
        if text and len(text) > 12:
            src = rt.get("FirstURL", "")
            return {"answer": text, "source": src, "kind": "DDG related"}
    return {}


# ──────────────────────────────────────────────────────────────────────────────
# Wikipedia REST summary (best for: people, places, things, concepts)
# ──────────────────────────────────────────────────────────────────────────────

def _wiki_search_title(query: str) -> str | None:
    """Find the best Wikipedia title for a free-form query."""
    titles = _wiki_search_titles(query, n=1)
    return titles[0] if titles else None


def _wiki_search_titles(query: str, n: int = 3) -> list[str]:
    """Find the top N best Wikipedia titles for a free-form query (V11)."""
    try:
        r = requests.get(
            WIKI_SRCH,
            params={
                "action": "query", "list": "search",
                "srsearch": query, "srlimit": n, "format": "json",
            },
            headers=UA, timeout=TIMEOUT,
        )
        r.raise_for_status()
        return [h["title"] for h in r.json().get("query", {}).get("search", []) if h.get("title")]
    except Exception as e:
        logger.info("Wiki search failed: %s", e)
        return []


def wikipedia_multi(query: str, n: int = 3, sentences: int = 3) -> list[dict]:
    """
    V11: return up to N Wikipedia summaries for the best-matching titles.
    Critical for ambiguous / specific queries where the FIRST hit doesn't
    contain the answer (the 'T1 LoL members' failure — top hit was the
    Worlds Championship article, but T1 (esports) was the 2nd or 3rd hit
    and DOES have the roster).
    """
    out: list[dict] = []
    seen: set[str] = set()
    for title in _wiki_search_titles(query, n=n):
        if title in seen:
            continue
        seen.add(title)
        try:
            url = WIKI_API.format(title=urllib.parse.quote(title.replace(" ", "_")))
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            if r.status_code != 200:
                continue
            data = r.json()
        except Exception as e:
            logger.debug("wiki_multi fetch failed for %s: %s", title, e)
            continue
        extract = (data.get("extract") or "").strip()
        if not extract:
            continue
        short = " ".join(re.split(r"(?<=[.!?])\s+", extract)[:sentences]).strip()
        out.append({
            "title":  data.get("title", title),
            "answer": short,
            "source": (data.get("content_urls", {})
                          .get("desktop", {}).get("page", "")),
        })
    return out


def wikipedia_summary(query: str, max_sentences: int = 2) -> dict:
    """
    Returns {'answer': str, 'title': str, 'source': URL} on hit, {} on miss.
    """
    if not query or not query.strip():
        return {}
    title = _wiki_search_title(query)
    if not title:
        return {}
    try:
        url = WIKI_API.format(title=urllib.parse.quote(title.replace(" ", "_")))
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        if r.status_code != 200:
            return {}
        data = r.json()
    except Exception as e:
        logger.info("Wiki summary failed: %s", e)
        return {}

    extract = (data.get("extract") or "").strip()
    if not extract:
        return {}
    # Trim to N sentences for voice readability
    sentences = re.split(r"(?<=[.!?])\s+", extract)
    short = " ".join(sentences[:max_sentences]).strip()
    return {
        "answer": short,
        "title":  data.get("title", title),
        "source": data.get("content_urls", {}).get("desktop", {}).get("page", ""),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Unified entry point used by brain.py
# ──────────────────────────────────────────────────────────────────────────────

def _clean_query(query: str) -> str:
    """
    V9: strip conversational fluff so DDG / Wikipedia get a clean search term.
    'hey maki tell me about barack obama please' -> 'barack obama'
    """
    q = (query or "").strip()
    q = re.sub(r"^(hey\s+)?maki[,\s]+", "", q, flags=re.I)
    q = re.sub(
        r"^(what'?s|whats|what\s+is|what\s+are|who'?s|who\s+is|who\s+was|"
        r"tell\s+me\s+(?:about|more\s+about)|tell\s+me|"
        r"give\s+me\s+(?:some\s+)?info(?:rmation)?\s+(?:about|on)|"
        r"do\s+you\s+know(?:\s+about)?|define|explain|describe|"
        r"can\s+you\s+(?:tell\s+me|explain|find|look\s+up)|"
        r"i\s+want\s+to\s+know(?:\s+about)?|look\s+up|"
        r"search\s+(?:for\s+|the\s+web\s+for\s+)?)\s+",
        "", q, flags=re.I,
    )
    q = re.sub(r"\s+(please|for\s+me|right\s+now|currently|thanks|thank\s+you)\s*$",
               "", q, flags=re.I)
    q = re.sub(r"^(a|an|the)\s+", "", q, flags=re.I)   # drop leading article
    return q.rstrip("?.!,; ").strip()


def live_lookup(query: str) -> dict:
    """
    V9 chain: Tavily → Brave → (DDG + Wikipedia merged) → {}.
    Returns {'answer','source','kind'} or {}. brain.py uses this to ANSWER
    inside the app instead of opening a browser.
    """
    if not query or not query.strip():
        return {}

    # V12: strip the model's stale-year bias ("2023" pollutes everything)
    query = _strip_stale_years(query)

    # ── 1. Tavily — best summaries + citations (key-gated) ───────────────
    res = tavily_search(query)
    if res.get("answer"):
        return res

    # ── 2. Brave — optional backup (key-gated) ───────────────────────────
    res = brave_search(query)
    if res.get("answer"):
        return res

    # ── 3. Free path: DDG Instant Answer + multi-Wikipedia, merged ───────
    cleaned = _clean_query(query)
    if not cleaned:
        return {}

    ddg   = ddg_instant_answer(cleaned)
    wikis = wikipedia_multi(cleaned, n=3, sentences=3)   # V11: up to 3 articles
    ddg_ans = (ddg.get("answer") or "").strip()

    # If only ONE solid source exists, return it cleanly.
    if not wikis and ddg_ans:
        return {"answer": ddg_ans,
                "source": ddg.get("source", "DuckDuckGo"),
                "kind": ddg.get("kind", "DuckDuckGo")}
    if not wikis and not ddg_ans:
        return {}

    # ── V11: multi-source MERGED CONTEXT ──────────────────────────────────
    # Give the model rich context to reason over instead of one snippet.
    # Critical for ambiguous queries (e.g. "T1 LoL members" — top hit was
    # Worlds Championship, but T1 (esports) is hit #2 with the actual roster).
    blocks: list[str] = []
    sources: list[str] = []
    if ddg_ans:
        blocks.append(f"[DuckDuckGo] {ddg_ans}")
        if ddg.get("source"):
            sources.append(str(ddg.get("source")))
    for w in wikis:
        title = w.get("title", "")
        ans   = w.get("answer", "").strip()
        src   = w.get("source", "")
        if not ans:
            continue
        blocks.append(f"[Wikipedia · {title}] {ans}")
        if src:
            sources.append(src)

    if not blocks:
        return {}
    # If only ONE block, return as a normal single answer.
    if len(blocks) == 1:
        only = blocks[0].split("] ", 1)[-1]
        return {"answer": only,
                "source": sources[0] if sources else "Wikipedia",
                "kind":   "Wikipedia"}
    # Multi-block — return both a short headline (first solid block) AND the
    # full merged context so the agent has real material to reason with.
    headline = blocks[0].split("] ", 1)[-1]
    context  = "\n\n".join(blocks)
    return {
        "answer":   _shorten(headline, 360),
        "context":  context,                # FULL multi-source context
        "source":   "Wikipedia · " + (wikis[0].get("title") if wikis else ""),
        "kind":     "Wikipedia",
        "sources":  sources,
    }


def provider_status() -> dict:
    """Show which live-search providers are configured (for diagnostics/UI)."""
    return {
        "tavily":     bool(TAVILY_API_KEY and TAVILY_ENABLED),
        "brave":      bool(BRAVE_SEARCH_KEY and BRAVE_ENABLED),
        "duckduckgo": True,
        "wikipedia":  True,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Browser fallbacks (last resort, only when AI / live tool truly can't answer)
# ──────────────────────────────────────────────────────────────────────────────

def open_google_search(query: str) -> str:
    if not query:
        return "What should I search for?"
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
    try:
        webbrowser.open(url)
        return f"Opening a Google search for '{query}'."
    except Exception:
        return "Couldn't open the browser."


def open_youtube_search(query: str) -> str:
    if not query:
        return "What should I search for on YouTube?"
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
    try:
        webbrowser.open(url)
        return f"Searching YouTube for '{query}'."
    except Exception:
        return "Couldn't open the browser."


def open_duckduckgo_search(query: str) -> str:
    if not query:
        return "What should I search for?"
    url = "https://duckduckgo.com/?q=" + urllib.parse.quote_plus(query)
    try:
        webbrowser.open(url)
        return f"Opening a DuckDuckGo search for '{query}'."
    except Exception:
        return "Couldn't open the browser."


# ──────────────────────────────────────────────────────────────────────────────
# Legacy current-info detector — kept for backward compatibility with brain.py.
# V7 prefers `needs_live_data()` above.
# ──────────────────────────────────────────────────────────────────────────────

def detect_current_info_question(text: str) -> bool:
    return needs_live_data(text)
