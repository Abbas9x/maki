"""
world_time_tools.py — V7 world time / timezone tool.

Covers a wide set of cities directly. For unknown places, falls back to
Open-Meteo geocoding (free, no key) to find a timezone.

Returns natural sentences ready for voice playback.
"""

import datetime, logging, re
import requests
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
TIMEOUT     = 4


# ── Curated map: place name → IANA timezone ───────────────────────────────────
_TZ = {
    # Pakistan
    "pakistan":"Asia/Karachi","karachi":"Asia/Karachi",
    "islamabad":"Asia/Karachi","lahore":"Asia/Karachi",
    "peshawar":"Asia/Karachi","rawalpindi":"Asia/Karachi",
    # Gulf
    "dubai":"Asia/Dubai","uae":"Asia/Dubai","abu dhabi":"Asia/Dubai",
    "sharjah":"Asia/Dubai",
    "saudi":"Asia/Riyadh","saudi arabia":"Asia/Riyadh",
    "riyadh":"Asia/Riyadh","jeddah":"Asia/Riyadh",
    "qatar":"Asia/Qatar","doha":"Asia/Qatar",
    "kuwait":"Asia/Kuwait",
    "bahrain":"Asia/Bahrain",
    "oman":"Asia/Muscat","muscat":"Asia/Muscat",
    # Turkey / Europe
    "turkey":"Europe/Istanbul","istanbul":"Europe/Istanbul","ankara":"Europe/Istanbul",
    "uk":"Europe/London","london":"Europe/London","england":"Europe/London",
    "britain":"Europe/London","great britain":"Europe/London",
    "manchester":"Europe/London","liverpool":"Europe/London",
    "ireland":"Europe/Dublin","dublin":"Europe/Dublin",
    "scotland":"Europe/London","edinburgh":"Europe/London","glasgow":"Europe/London",
    "germany":"Europe/Berlin","berlin":"Europe/Berlin","munich":"Europe/Berlin",
    "france":"Europe/Paris","paris":"Europe/Paris","lyon":"Europe/Paris",
    "spain":"Europe/Madrid","madrid":"Europe/Madrid","barcelona":"Europe/Madrid",
    "italy":"Europe/Rome","rome":"Europe/Rome","milan":"Europe/Rome",
    "netherlands":"Europe/Amsterdam","amsterdam":"Europe/Amsterdam",
    "russia":"Europe/Moscow","moscow":"Europe/Moscow",
    # USA — east
    "new york":"America/New_York","nyc":"America/New_York","new york city":"America/New_York",
    "boston":"America/New_York","miami":"America/New_York","atlanta":"America/New_York",
    "washington":"America/New_York","philadelphia":"America/New_York",
    "est":"America/New_York","eastern":"America/New_York",
    # USA — central
    "chicago":"America/Chicago","texas":"America/Chicago",
    "dallas":"America/Chicago","houston":"America/Chicago",
    "baytown":"America/Chicago","pasadena":"America/Chicago",  # both TX-Chicago zones
    "san antonio":"America/Chicago","austin":"America/Chicago",
    "new orleans":"America/Chicago","memphis":"America/Chicago",
    "minneapolis":"America/Chicago","st louis":"America/Chicago",
    "cst":"America/Chicago","central":"America/Chicago",
    # USA — mountain
    "denver":"America/Denver","salt lake city":"America/Denver","mst":"America/Denver",
    "phoenix":"America/Phoenix",  # no DST
    # USA — west
    "los angeles":"America/Los_Angeles","la":"America/Los_Angeles",
    "san francisco":"America/Los_Angeles","seattle":"America/Los_Angeles",
    "portland":"America/Los_Angeles","san diego":"America/Los_Angeles",
    "pst":"America/Los_Angeles","pacific":"America/Los_Angeles",
    # Canada
    "toronto":"America/Toronto","ottawa":"America/Toronto","montreal":"America/Toronto",
    "vancouver":"America/Vancouver","calgary":"America/Edmonton","edmonton":"America/Edmonton",
    "winnipeg":"America/Winnipeg","halifax":"America/Halifax",
    # India / Asia
    "india":"Asia/Kolkata","mumbai":"Asia/Kolkata","delhi":"Asia/Kolkata",
    "bangalore":"Asia/Kolkata","new delhi":"Asia/Kolkata","chennai":"Asia/Kolkata",
    "japan":"Asia/Tokyo","tokyo":"Asia/Tokyo","osaka":"Asia/Tokyo","kyoto":"Asia/Tokyo",
    "china":"Asia/Shanghai","beijing":"Asia/Shanghai","shanghai":"Asia/Shanghai",
    "hong kong":"Asia/Hong_Kong","singapore":"Asia/Singapore",
    "korea":"Asia/Seoul","south korea":"Asia/Seoul","seoul":"Asia/Seoul",
    "thailand":"Asia/Bangkok","bangkok":"Asia/Bangkok",
    "malaysia":"Asia/Kuala_Lumpur","kuala lumpur":"Asia/Kuala_Lumpur",
    "philippines":"Asia/Manila","manila":"Asia/Manila",
    "indonesia":"Asia/Jakarta","jakarta":"Asia/Jakarta",
    # Oceania
    "sydney":"Australia/Sydney","melbourne":"Australia/Melbourne",
    "brisbane":"Australia/Brisbane","perth":"Australia/Perth",
    "new zealand":"Pacific/Auckland","auckland":"Pacific/Auckland",
    # Latin America
    "brazil":"America/Sao_Paulo","sao paulo":"America/Sao_Paulo","rio":"America/Sao_Paulo",
    "argentina":"America/Argentina/Buenos_Aires","buenos aires":"America/Argentina/Buenos_Aires",
    "mexico city":"America/Mexico_City","tijuana":"America/Tijuana",
    "chile":"America/Santiago","santiago":"America/Santiago",
    "colombia":"America/Bogota","bogota":"America/Bogota",
    # Africa
    "south africa":"Africa/Johannesburg","johannesburg":"Africa/Johannesburg",
    "egypt":"Africa/Cairo","cairo":"Africa/Cairo",
    "nigeria":"Africa/Lagos","lagos":"Africa/Lagos",
    "kenya":"Africa/Nairobi","nairobi":"Africa/Nairobi",
}

# Countries with multiple time zones → ask which city
_MULTI_TZ = {
    "canada":   "Toronto, Vancouver, Calgary or Halifax",
    "usa":      "New York, Los Angeles, Chicago or Denver",
    "us":       "New York, Los Angeles, Chicago or Denver",
    "america":  "New York, Los Angeles, Chicago or Denver",
    "australia":"Sydney, Melbourne, Perth or Brisbane",
    # "mexico" without "city" → ambiguous (CT vs PT) → ask
    "mexico":   "Mexico City or Tijuana",
}

_DAYS = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]


# ──────────────────────────────────────────────────────────────────────────────
# Lookup
# ──────────────────────────────────────────────────────────────────────────────

def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def is_ambiguous_country(name: str) -> str | None:
    """If user said a multi-tz country, return hint string; else None."""
    return _MULTI_TZ.get(_normalize(name))


def _resolve_tz(place: str) -> tuple[str, str] | None:
    """
    Resolve a place name → (IANA_tz, display_name).
    Step 1: curated map (instant).
    Step 2: Open-Meteo geocoding (free, returns the place's timezone).
    """
    key = _normalize(place)
    if not key:
        return None
    if key in _TZ:
        return _TZ[key], place.title()

    # Substring match against curated map (handles things like "in the UK")
    for alias, tz in _TZ.items():
        if alias in key or key in alias:
            return tz, place.title()

    # Geocode fallback
    try:
        r = requests.get(
            GEOCODE_URL,
            params={"name": place, "count": 1, "language": "en", "format": "json"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        hits = r.json().get("results") or []
        if hits:
            top = hits[0]
            tz  = top.get("timezone")
            if tz:
                display = top.get("name", place)
                country = top.get("country", "")
                if country and country != display:
                    display = f"{display}, {country}"
                return tz, display
    except Exception as e:
        logger.info("Geocode timezone failed for %r: %s", place, e)
    return None


def get_time_in(place: str, offset_hours: float = 0.0) -> dict:
    """
    Return current (or offset) time in a place.
    Returns {'time','date','day','location','timezone'} or {'error': ...}.
    """
    if not place or not place.strip():
        return {"error": "no place given"}

    # Country-level ambiguity → tell caller to ask user
    hint = is_ambiguous_country(place)
    if hint:
        return {"ambiguous": True, "hint": hint, "country": place.title()}

    resolved = _resolve_tz(place)
    if not resolved:
        return {"error": f"Couldn't find a timezone for '{place}'."}
    tz_name, display = resolved
    try:
        now = datetime.datetime.now(ZoneInfo(tz_name))
        if offset_hours:
            now = now + datetime.timedelta(hours=offset_hours)
        h    = now.strftime("%I").lstrip("0") or "12"
        m    = now.strftime("%M")
        ampm = now.strftime("%p")
        t    = f"{h}:{m} {ampm}" if m != "00" else f"{h} {ampm}"
        day  = _DAYS[now.weekday()]
        date = now.strftime("%B %d")
        return {
            "time":     t,
            "date":     date,
            "day":      day,
            "location": display,
            "timezone": tz_name,
        }
    except Exception as e:
        return {"error": str(e)}


def speak_time_in(place: str, offset_hours: float = 0.0) -> str:
    """High-level voice-ready sentence."""
    res = get_time_in(place, offset_hours)
    if res.get("ambiguous"):
        return (f"{res['country']} spans multiple time zones — "
                f"which city? For example: {res['hint']}.")
    if "error" in res:
        return (f"I don't have a timezone for {place.title()}. "
                f"Try a specific city like London, Tokyo, or New York.")
    when = "right now" if not offset_hours else f"in {int(offset_hours)} hours"
    return f"It'll be {res['time']} in {res['location']} {when}." if offset_hours \
        else f"It's {res['time']} in {res['location']} right now."


def speak_day_in(place: str) -> str:
    """What day is it in <place>?"""
    res = get_time_in(place)
    if res.get("ambiguous"):
        return (f"{res['country']} has multiple time zones — "
                f"which city? Try: {res['hint']}.")
    if "error" in res:
        return f"I'm not sure of the timezone for {place.title()}."
    return f"It's {res['day']}, {res['date']} in {res['location']}."
