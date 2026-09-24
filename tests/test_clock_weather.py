"""clock.* and weather.*, with the network replaced by canned answers."""

from datetime import datetime

import pytest

from arnold.commands import build_registry
from arnold.commands import weather as weather_mod
from arnold.commands.clock import date_speech, now_sentence
from arnold.commands.registry import CommandContext
from arnold.config import Config

FORECAST = {
    "current_units": {"temperature_2m": "°F", "wind_speed_10m": "mp/h"},
    "current": {
        "temperature_2m": 77.7, "apparent_temperature": 86.7, "relative_humidity_2m": 91,
        "weather_code": 3, "wind_speed_10m": 4.1, "precipitation": 0.0, "is_day": 0,
    },
    "daily": {
        "time": ["2026-09-23", "2026-09-24", "2026-09-25"],
        "weather_code": [61, 3, 95],
        "temperature_2m_max": [81.8, 86.1, 84.0],
        "temperature_2m_min": [75.3, 75.0, 74.2],
        "precipitation_probability_max": [65, 10, 80],
        "sunrise": ["2026-09-23T07:10", "2026-09-24T07:10", "2026-09-25T07:11"],
        "sunset": ["2026-09-23T19:15", "2026-09-24T19:14", "2026-09-25T19:13"],
    },
}
IP = {"loc": "25.77,-80.19", "city": "Miami", "region": "Florida", "country": "US"}
GEO = {"results": [
    {"name": "London", "admin1": "Ontario", "country": "Canada", "country_code": "CA",
     "latitude": 42.98, "longitude": -81.23},
    {"name": "London", "admin1": "England", "country": "United Kingdom", "country_code": "GB",
     "latitude": 51.51, "longitude": -0.13},
]}


@pytest.fixture
def net(monkeypatch):
    calls = []

    def fake(url, params=None):
        calls.append((url, params or {}))
        if url == weather_mod.IP_LOCATE_URL:
            return IP
        if url == weather_mod.GEOCODE_URL:
            return GEO
        return FORECAST

    monkeypatch.setattr(weather_mod, "_get_json", fake)
    weather_mod._places.clear()
    weather_mod._forecasts.clear()
    return calls


def _run(name, args=None, config=None):
    ctx = CommandContext(config=config or Config(), collector=None, alerts=None)
    return build_registry().dispatch(name, args or {}, ctx)


def test_clock_now_says_the_time_and_date():
    result = _run("clock.now")
    assert result.ok
    assert result.speech.startswith("It's ")
    assert datetime.now().strftime("%A") in result.speech
    assert result.result["date"] == datetime.now().date().isoformat()


def test_date_speech_uses_ordinals():
    assert date_speech(datetime(2026, 9, 23)) == "Wednesday, September 23rd, 2026"
    assert date_speech(datetime(2026, 9, 11)) == "Friday, September 11th, 2026"
    assert "2026" in now_sentence()


def test_weather_now_locates_by_ip_and_speaks_in_fahrenheit(net):
    result = _run("weather.now")
    assert result.ok, result.error
    assert result.speech.startswith("It's 78 degrees with overcast skies, feeling like 87.")
    assert "65 percent chance of rain" in result.speech
    assert result.result["place"] == "Miami, Florida"
    forecast = [p for u, p in net if u == weather_mod.FORECAST_URL][0]
    assert forecast["temperature_unit"] == "fahrenheit"


def test_a_named_place_keeps_home_units_and_honours_the_qualifier(net):
    result = _run("weather.forecast", {"place": "London, UK", "days": 2})
    assert result.ok, result.error
    assert result.result["place"] == "London, England, United Kingdom"
    assert result.speech.startswith("Today in London,")
    forecast = [p for u, p in net if u == weather_mod.FORECAST_URL][0]
    assert forecast["latitude"] == 51.51
    assert forecast["temperature_unit"] == "fahrenheit"  # home is the US


def test_forecast_for_one_day(net):
    result = _run("weather.forecast", {"day": "friday"})
    assert result.ok, result.error
    assert result.speech == (
        "Friday, thunderstorms, a high of 84 degrees and a low of 74 degrees, "
        "with a 80 percent chance of rain."
    )


def test_config_place_and_units_win(net):
    config = Config()
    config.weather.place = "London, UK"
    config.weather.units = "metric"
    assert _run("weather.now", config=config).ok
    assert not any(u == weather_mod.IP_LOCATE_URL for u, _ in net)
    forecast = [p for u, p in net if u == weather_mod.FORECAST_URL][0]
    assert forecast["temperature_unit"] == "celsius"


def test_forecasts_are_cached(net):
    _run("weather.now")
    _run("weather.forecast", {"day": "tomorrow"})
    assert sum(1 for u, _ in net if u == weather_mod.FORECAST_URL) == 1


def test_an_unreachable_service_is_a_spoken_failure(monkeypatch):
    def down(url, params=None):
        raise weather_mod.WeatherError("I can't reach the weather service right now.")

    monkeypatch.setattr(weather_mod, "_get_json", down)
    weather_mod._places.clear()
    result = _run("weather.now")
    assert not result.ok
    assert result.speech == "I can't reach the weather service right now."
