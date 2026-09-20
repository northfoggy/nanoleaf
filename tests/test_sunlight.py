from datetime import date, datetime, timezone

import pytest
import requests

from nanoleaf_ctl import sunlight
from nanoleaf_ctl.weather import WeatherState


class _Device:
    url = "http://device.test/api/v1/token"


class _FailedResponse:
    def raise_for_status(self):
        raise requests.HTTPError("device rejected request")


def test_solar_events_use_configured_local_date(monkeypatch):
    cfg = sunlight.WindowConfig(
        latitude=35.7,
        longitude=139.7,
        timezone="Asia/Tokyo",
        facing="east",
    )
    captured = {}

    monkeypatch.setattr(sunlight, "_solar_elevation", lambda *_: 3.0)
    monkeypatch.setattr(sunlight, "_solar_azimuth", lambda *_: 90.0)

    def sun_times(_cfg, local_date):
        captured["date"] = local_date
        return {"noon": datetime(2026, 1, 2, 3, tzinfo=timezone.utc)}

    monkeypatch.setattr(sunlight, "_sun_times", sun_times)

    state = sunlight.compute_window_light(
        cfg, datetime(2026, 1, 1, 21, tzinfo=timezone.utc),
    )

    assert captured["date"] == date(2026, 1, 2)
    assert state["phase"] == "golden hour"


def test_apply_light_raises_on_http_failure(monkeypatch):
    monkeypatch.setattr(sunlight.requests, "put", lambda *args, **kwargs: _FailedResponse())

    with pytest.raises(requests.HTTPError):
        sunlight.apply_light(
            _Device(), {"mode": "off", "brightness": 0},
        )


def test_weather_does_not_turn_night_state_back_on():
    weather = WeatherState(
        cloud_cover=100,
        condition="overcast",
        is_day=False,
        timestamp=123.0,
    )

    state = sunlight.apply_weather(
        {"mode": "off", "brightness": 0, "phase": "night"}, weather,
    )

    assert state["mode"] == "off"
    assert state["brightness"] == 0
    assert state["weather"] == "overcast"
    assert state["brightness_source"] == "astronomical"


def test_window_irradiance_is_primary_brightness_input():
    weather = WeatherState(
        cloud_cover=100,
        condition="overcast",
        is_day=True,
        timestamp=123.0,
        irradiance_w_m2=175.0,
        shortwave_radiation_w_m2=300.0,
        irradiance_orientation="southwest",
    )

    state = sunlight.apply_weather(
        {
            "mode": "color_temp", "brightness": 80,
            "color_temp": 5000, "phase": "day",
        },
        weather,
    )

    # 175 W/m² is one quarter of the 700 W/m² full scale; the perceptual
    # square-root curve therefore caps brightness at 50%.
    assert state["brightness"] == 50
    assert state["brightness_source"] == "open-meteo-irradiance"
    assert state["irradiance_w_m2"] == 175.0
    assert state["irradiance_orientation"] == "southwest"
    assert state["weather_source"] == "open-meteo"
    # Cloud cover remains a secondary color input.
    assert state["color_temp"] == 5500


def test_irradiance_never_brightens_above_astronomical_state():
    weather = WeatherState(
        cloud_cover=0,
        condition="clear",
        is_day=True,
        timestamp=123.0,
        irradiance_w_m2=700.0,
    )

    state = sunlight.apply_weather(
        {
            "mode": "color_temp", "brightness": 30,
            "color_temp": 3500, "phase": "morning",
        },
        weather,
    )

    assert state["brightness"] == 30


def test_missing_irradiance_uses_legacy_cloud_cover_fallback():
    weather = WeatherState(
        cloud_cover=100,
        condition="overcast",
        is_day=True,
        timestamp=123.0,
    )

    state = sunlight.apply_weather(
        {
            "mode": "color_temp", "brightness": 80,
            "color_temp": 5000, "phase": "day",
        },
        weather,
    )

    assert state["brightness"] == 48
    assert state["brightness_source"] == "cloud-cover-fallback"
    assert "irradiance_w_m2" not in state


def test_predawn_color_uses_cloud_fallback_instead_of_zero_irradiance():
    weather = WeatherState(
        cloud_cover=50,
        condition="partly_cloudy",
        is_day=False,
        timestamp=123.0,
        irradiance_w_m2=0.0,
    )

    state = sunlight.apply_weather(
        {
            "mode": "color", "brightness": 20,
            "rgb": (100, 80, 60), "phase": "blue hour",
        },
        weather,
    )

    assert state["brightness"] == 16
    assert state["brightness_source"] == "cloud-cover-fallback"
