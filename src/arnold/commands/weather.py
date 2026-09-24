"""The weather, from Open-Meteo.

Open-Meteo needs no key and no account, which suits a machine that should
just answer. The location is, in order: a place the caller names, the
coordinates or place in config.yaml, and finally this PC's public IP -
city-level, and good enough to say whether to take a coat.

Lookups are cached: the same forecast is asked for in bursts ("and
tomorrow?"), and a home's location does not move between questions.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .registry import CommandContext, CommandError, CommandResult, Registry, arg_int

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
IP_LOCATE_URL = "https://ipinfo.io/json"
TIMEOUT = 8.0
FORECAST_TTL = 10 * 60
LOCATION_TTL = 6 * 60 * 60
# Where Fahrenheit is what people mean by a temperature.
IMPERIAL_COUNTRIES = {"US", "LR", "MM", "PR", "GU", "VI", "AS", "MP", "BS", "KY", "PW", "FM", "MH"}

# How people qualify a place ("London, UK") versus the ISO codes the
# geocoder reports.
COUNTRY_ALIASES = {
    "uk": "gb", "britain": "gb", "great britain": "gb", "england": "gb",
    "scotland": "gb", "wales": "gb", "usa": "us", "america": "us",
    "united states": "us", "us": "us",
}

# WMO weather interpretation codes, as Open-Meteo reports them.
CONDITIONS = {
    0: "clear skies", 1: "mostly clear skies", 2: "partly cloudy skies", 3: "overcast skies",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "light snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with heavy hail",
}


def register_all(registry: Registry) -> None:
    registry.register(
        "weather.now",
        _now,
        "Current weather: conditions, temperature, feels-like, wind, and today's high and low.",
        {"place": "optional town or city; blank = here"},
    )
    registry.register(
        "weather.forecast",
        _forecast,
        "Daily forecast: highs, lows, conditions and chance of rain.",
        {
            "place": "optional town or city; blank = here",
            "day": "optional: today, tomorrow or a weekday name",
            "days": "how many days from today, 1-7 (default 3)",
        },
    )


class WeatherError(CommandError):
    pass


@dataclass(slots=True)
class Place:
    name: str
    latitude: float
    longitude: float
    country: str = ""


_lock = threading.Lock()
_places: dict[str, tuple[float, Place]] = {}
_forecasts: dict[tuple, tuple[float, dict]] = {}


def _get_json(url: str, params: dict[str, Any] | None = None) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "arnold-desktop-agent"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WeatherError("I can't reach the weather service right now.") from exc
    except ValueError as exc:
        raise WeatherError("The weather service sent back something I couldn't read.") from exc


def _cached_place(key: str) -> Place | None:
    with _lock:
        hit = _places.get(key)
    if hit and time.time() - hit[0] < LOCATION_TTL:
        return hit[1]
    return None


def _remember_place(key: str, place: Place) -> Place:
    with _lock:
        _places[key] = (time.time(), place)
    return place


def geocode(query: str) -> Place:
    key = "q:" + query.strip().lower()
    if cached := _cached_place(key):
        return cached
    # The geocoder matches a bare name; "Leeds, UK" is split so the
    # qualifier narrows the match instead of failing it.
    name, _, qualifier = query.partition(",")
    data = _get_json(GEOCODE_URL, {"name": name.strip(), "count": 10, "language": "en"})
    results = data.get("results") or []
    qualifier = qualifier.strip().lower().rstrip(".")
    if qualifier:
        code = COUNTRY_ALIASES.get(qualifier.replace(".", ""), "")

        def matches(r: dict) -> bool:
            if code and str(r.get("country_code") or "").lower() == code:
                return True
            if len(qualifier) == 2:  # a country or state code, never a substring
                return qualifier == str(r.get("country_code") or "").lower()
            return qualifier in " ".join(
                str(r.get(k) or "") for k in ("country", "admin1", "admin2")
            ).lower()

        narrowed = [r for r in results if matches(r)]
        results = narrowed or results
    if not results:
        raise WeatherError(f"I couldn't find a place called {query}.")
    best = results[0]
    label = ", ".join(
        part for part in (best.get("name"), best.get("admin1"), best.get("country")) if part
    )
    return _remember_place(
        key,
        Place(label, float(best["latitude"]), float(best["longitude"]), str(best.get("country_code") or "")),
    )


def locate_by_ip() -> Place:
    if cached := _cached_place("ip"):
        return cached
    data = _get_json(IP_LOCATE_URL)
    try:
        lat, lon = (float(v) for v in str(data["loc"]).split(","))
    except (KeyError, ValueError) as exc:
        raise WeatherError(
            "I couldn't work out where this PC is. Set weather.place in config.yaml."
        ) from exc
    label = ", ".join(part for part in (data.get("city"), data.get("region")) if part) or "here"
    return _remember_place("ip", Place(label, lat, lon, str(data.get("country") or "")))


def resolve_place(config, asked: str = "") -> Place:
    cfg = config.weather
    if asked:
        return geocode(asked)
    if cfg.latitude is not None and cfg.longitude is not None:
        return Place(cfg.place or "here", float(cfg.latitude), float(cfg.longitude))
    if cfg.place.strip():
        return geocode(cfg.place)
    return locate_by_ip()


def imperial(config, place: Place) -> bool:
    units = (config.weather.units or "auto").strip().lower()
    if units in ("imperial", "fahrenheit", "us"):
        return True
    if units in ("metric", "celsius"):
        return False
    # The units of home, not of the place asked about: someone in Miami
    # asking about London still thinks in Fahrenheit.
    try:
        home = resolve_place(config)
        country = home.country or locate_by_ip().country
    except WeatherError:
        country = place.country
    return country.upper() in IMPERIAL_COUNTRIES


def fetch(place: Place, use_imperial: bool) -> dict:
    key = (round(place.latitude, 3), round(place.longitude, 3), use_imperial)
    with _lock:
        hit = _forecasts.get(key)
    if hit and time.time() - hit[0] < FORECAST_TTL:
        return hit[1]
    params = {
        "latitude": place.latitude,
        "longitude": place.longitude,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
        "weather_code,wind_speed_10m,precipitation,is_day",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
        "precipitation_probability_max,sunrise,sunset",
        "timezone": "auto",
        "forecast_days": 7,
        "temperature_unit": "fahrenheit" if use_imperial else "celsius",
        "wind_speed_unit": "mph" if use_imperial else "kmh",
        "precipitation_unit": "inch" if use_imperial else "mm",
    }
    data = _get_json(FORECAST_URL, params)
    if "current" not in data or "daily" not in data:
        raise WeatherError(str(data.get("reason") or "The weather service had no forecast for there."))
    with _lock:
        _forecasts[key] = (time.time(), data)
    return data


def _opt(args: dict, key: str) -> str:
    return str(args.get(key) or "").strip()


def _condition(code: Any) -> str:
    try:
        return CONDITIONS.get(int(code), "unsettled weather")
    except (TypeError, ValueError):
        return "unsettled weather"


def _deg(value: Any) -> str:
    return f"{round(float(value))} degrees"


def _where(place: Place, asked: str) -> str:
    """'in Leeds' when a place was asked for; nothing for here."""
    if not asked:
        return ""
    return f" in {place.name.split(',')[0]}"


def _clock(text: str) -> str:
    when = datetime.fromisoformat(text)
    hour = when.hour % 12 or 12
    return f"{hour}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'}"


def _days(data: dict) -> list[dict]:
    daily = data["daily"]
    out = []
    for i, day in enumerate(daily.get("time") or []):
        def at(name):
            values = daily.get(name) or []
            return values[i] if i < len(values) else None

        out.append(
            {
                "date": day,
                "weekday": date.fromisoformat(day).strftime("%A"),
                "conditions": _condition(at("weather_code")),
                "high": at("temperature_2m_max"),
                "low": at("temperature_2m_min"),
                "rain_chance": at("precipitation_probability_max"),
                "sunrise": at("sunrise"),
                "sunset": at("sunset"),
            }
        )
    return out


def _units(data: dict) -> dict:
    units = data.get("current_units") or {}
    return {"temperature": units.get("temperature_2m", ""), "wind": units.get("wind_speed_10m", "")}


def _day_speech(day: dict, label: str) -> str:
    text = f"{label}, {day['conditions']}, a high of {_deg(day['high'])} and a low of {_deg(day['low'])}"
    chance = day.get("rain_chance")
    if chance is not None and chance >= 20:
        text += f", with a {round(chance)} percent chance of rain"
    return text


def _now(ctx: CommandContext, args: dict) -> CommandResult:
    if not ctx.config.weather.enabled:
        raise CommandError("Weather is switched off in config.yaml.")
    asked = _opt(args, "place")
    place = resolve_place(ctx.config, asked)
    data = fetch(place, imperial(ctx.config, place))
    current = data["current"]
    today = _days(data)[0]
    units = _units(data)
    temp, feels = current.get("temperature_2m"), current.get("apparent_temperature")
    conditions = _condition(current.get("weather_code"))
    speech = f"It's {_deg(temp)}{_where(place, asked)} with {conditions}"
    if feels is not None and abs(float(feels) - float(temp)) >= 3:
        speech += f", feeling like {round(float(feels))}"
    speech += f". Today's high is {_deg(today['high'])} and the low {_deg(today['low'])}"
    chance = today.get("rain_chance")
    if chance is not None and chance >= 20:
        speech += f", with a {round(chance)} percent chance of rain"
    speech += "."
    return CommandResult(
        speech=speech,
        result={
            "place": place.name,
            "conditions": conditions,
            "temperature": temp,
            "feels_like": feels,
            "humidity_percent": current.get("relative_humidity_2m"),
            "wind_speed": current.get("wind_speed_10m"),
            "precipitation": current.get("precipitation"),
            "is_day": bool(current.get("is_day")),
            "units": units,
            "today": today,
            "sunrise": _clock(today["sunrise"]) if today.get("sunrise") else None,
            "sunset": _clock(today["sunset"]) if today.get("sunset") else None,
        },
    )


def _pick_day(days: list[dict], wanted: str) -> int:
    wanted = wanted.strip().lower()
    if wanted in ("", "today", "tonight"):
        return 0
    if wanted == "tomorrow":
        return 1
    for i, day in enumerate(days):
        if day["weekday"].lower().startswith(wanted[:3]):
            return i
    raise CommandError(f"I only have the forecast a week ahead, and {wanted} isn't in it.")


def _forecast(ctx: CommandContext, args: dict) -> CommandResult:
    if not ctx.config.weather.enabled:
        raise CommandError("Weather is switched off in config.yaml.")
    asked = _opt(args, "place")
    place = resolve_place(ctx.config, asked)
    data = fetch(place, imperial(ctx.config, place))
    days = _days(data)
    where = _where(place, asked)
    result = {"place": place.name, "units": _units(data)}

    wanted = _opt(args, "day")
    if wanted:
        i = _pick_day(days, wanted)
        label = "Today" if i == 0 else "Tomorrow" if i == 1 else days[i]["weekday"]
        return CommandResult(
            speech=_day_speech(days[i], label + where) + ".",
            result={**result, "days": [days[i]]},
        )

    count = arg_int(args, "days", 3, minimum=1, maximum=7)
    chosen = days[:count]
    labels = ["Today", "tomorrow"] + [d["weekday"] for d in chosen[2:]]
    parts = [_day_speech(day, labels[i]) for i, day in enumerate(chosen)]
    parts[0] = parts[0].replace("Today", "Today" + where, 1)
    return CommandResult(speech="; ".join(parts) + ".", result={**result, "days": chosen})
