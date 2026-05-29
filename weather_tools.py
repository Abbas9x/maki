"""
weather_tools.py — Live weather via Open-Meteo (no API key required).

Two-step flow:
  1. Geocode the city name → lat/lon (Open-Meteo geocoding API)
  2. Fetch current weather at those coords (Open-Meteo forecast API)

Returns a natural spoken sentence so Maki can answer in voice without
opening a browser. Falls back gracefully on network errors.
"""

import logging
import requests

logger = logging.getLogger(__name__)

GEOCODE_URL  = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT      = 4  # short — we don't want to block the user

# WMO weather code → human description
# https://open-meteo.com/en/docs (under "Weather code")
_WMO = {
    0:  "clear",            1: "mostly clear",  2: "partly cloudy",  3: "overcast",
    45: "foggy",            48: "icy fog",
    51: "light drizzle",    53: "drizzling",    55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain",       63: "raining",      65: "heavy rain",
    66: "freezing rain",    67: "freezing rain",
    71: "light snow",       73: "snowing",      75: "heavy snow",
    77: "snow grains",
    80: "rain showers",     81: "rain showers", 82: "heavy showers",
    85: "snow showers",     86: "heavy snow showers",
    95: "thunderstorms",    96: "thunderstorms with hail",
    99: "severe thunderstorms",
}

# Country/region aliases → preferred city for geocoding
_COUNTRY_ALIAS = {
    "uk":         "London",
    "england":    "London",
    "britain":    "London",
    "great britain": "London",
    "scotland":   "Edinburgh",
    "uae":        "Dubai",
    "pakistan":   "Karachi",
    "saudi":      "Riyadh",
    "saudi arabia": "Riyadh",
    "japan":      "Tokyo",
    "france":     "Paris",
    "germany":    "Berlin",
    "italy":      "Rome",
    "spain":      "Madrid",
    "russia":     "Moscow",
    "china":      "Beijing",
    "india":      "Mumbai",
}


def _resolve_city(name: str) -> str:
    """Map vague names like 'England' → 'London' before geocoding."""
    key = (name or "").strip().lower()
    return _COUNTRY_ALIAS.get(key, name.strip())


def _geocode(city: str):
    """Look up lat/lon for a city. Returns (lat, lon, display_name) or None."""
    try:
        r = requests.get(
            GEOCODE_URL,
            params={"name": city, "count": 1, "language": "en", "format": "json"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        results = data.get("results") or []
        if not results:
            return None
        top = results[0]
        display = top.get("name", city)
        if top.get("admin1") and top["admin1"] != display:
            display = f"{display}, {top['admin1']}"
        elif top.get("country") and top["country"] != display:
            display = f"{display}, {top['country']}"
        return top["latitude"], top["longitude"], display
    except Exception as e:
        logger.warning("Geocode failed for %r: %s", city, e)
        return None


def get_weather(city: str, units: str = "fahrenheit") -> dict:
    """
    Fetch current weather for a city. Returns:
        {"summary": "It's about 82°F in Houston, mostly clear.", ...}
    or {"error": "..."} on failure.
    """
    if not city or not city.strip():
        return {"error": "no city given"}

    resolved = _resolve_city(city)
    geo = _geocode(resolved)
    if not geo:
        return {"error": f"Couldn't find a place called '{city}'."}

    lat, lon, display = geo
    temp_unit = "fahrenheit" if units.lower().startswith("f") else "celsius"
    wind_unit = "mph" if temp_unit == "fahrenheit" else "kmh"

    try:
        r = requests.get(
            FORECAST_URL,
            params={
                "latitude":          lat,
                "longitude":         lon,
                "current":           "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                "temperature_unit":  temp_unit,
                "wind_speed_unit":   wind_unit,
                "timezone":          "auto",
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        cur = r.json().get("current") or {}
    except Exception as e:
        logger.warning("Forecast fetch failed for %s: %s", display, e)
        return {"error": "weather service unreachable"}

    temp     = cur.get("temperature_2m")
    humidity = cur.get("relative_humidity_2m")
    code     = cur.get("weather_code")
    wind     = cur.get("wind_speed_10m")

    if temp is None:
        return {"error": "no current data"}

    desc      = _WMO.get(code, "")
    unit_sym  = "°F" if temp_unit == "fahrenheit" else "°C"
    wind_sym  = "mph" if wind_unit == "mph" else "km/h"

    # Build a natural sentence
    parts = [f"It's about {round(temp)}{unit_sym} in {display}"]
    if desc:
        parts.append(f", {desc}")
    if wind is not None and wind >= 12:
        parts.append(f", with {round(wind)} {wind_sym} winds")
    elif humidity is not None and humidity >= 80:
        parts.append(f", humid")
    parts.append(".")

    return {
        "summary":    "".join(parts),
        "temp":       temp,
        "unit":       unit_sym,
        "condition":  desc,
        "humidity":   humidity,
        "wind":       wind,
        "location":   display,
    }
