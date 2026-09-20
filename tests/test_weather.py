import requests

from nanoleaf_ctl import weather


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_fetch_weather_requests_vertical_window_irradiance(monkeypatch):
    request = {}

    def get(url, **kwargs):
        request["url"] = url
        request.update(kwargs)
        return _Response({
            "current": {
                "cloud_cover": 27,
                "weather_code": 1,
                "is_day": 1,
                "shortwave_radiation": 332,
                "direct_radiation": 242,
                "diffuse_radiation": 90,
                "global_tilted_irradiance": 78.2,
            },
        })

    monkeypatch.setattr(weather.requests, "get", get)
    monkeypatch.setattr(weather.time, "time", lambda: 1234.0)

    result = weather.fetch_weather(34.13, -84.34, "southwest")

    assert request["url"] == "https://api.open-meteo.com/v1/forecast"
    assert request["params"]["tilt"] == 90
    assert request["params"]["azimuth"] == 45
    assert "global_tilted_irradiance" in request["params"]["current"]
    assert request["timeout"] == 10
    assert result.irradiance_w_m2 == 78.2
    assert result.shortwave_radiation_w_m2 == 332.0
    assert result.direct_radiation_w_m2 == 242.0
    assert result.diffuse_radiation_w_m2 == 90.0
    assert result.irradiance_orientation == "southwest"
    assert result.timestamp == 1234.0


def test_fetch_weather_tolerates_missing_or_invalid_irradiance(monkeypatch):
    monkeypatch.setattr(
        weather.requests,
        "get",
        lambda *_args, **_kwargs: _Response({
            "current": {
                "cloud_cover": 80,
                "weather_code": 3,
                "is_day": 1,
                "global_tilted_irradiance": "unavailable",
            },
        }),
    )

    result = weather.fetch_weather(1.0, 2.0, "east")

    assert result.condition == "overcast"
    assert result.irradiance_w_m2 is None
    assert result.irradiance_orientation == "east"


def test_orientation_azimuth_uses_open_meteo_convention():
    assert weather._orientation_azimuth("south") == 0
    assert weather._orientation_azimuth("east") == -90
    assert weather._orientation_azimuth("southwest") == 45
    assert weather._orientation_azimuth("north") == 180


def test_failed_weather_fetch_waits_before_retry(monkeypatch):
    now = [1000.0]
    calls = []
    monkeypatch.setattr(weather.time, "time", lambda: now[0])

    def fail(*_args):
        calls.append(now[0])
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(weather, "fetch_weather", fail)
    cache = weather.WeatherCache(1.0, 2.0, refresh_interval=600)

    assert cache.get() is None
    now[0] += 60
    assert cache.get() is None
    assert calls == [1000.0]

    now[0] += 540
    assert cache.get() is None
    assert calls == [1000.0, 1600.0]


def test_failed_refresh_retains_last_good_observation(monkeypatch):
    now = [1000.0]
    calls = []
    first = weather.WeatherState(
        cloud_cover=20,
        condition="mostly_clear",
        is_day=True,
        timestamp=1000.0,
        irradiance_w_m2=250.0,
    )
    monkeypatch.setattr(weather.time, "time", lambda: now[0])

    def fetch(*_args):
        calls.append(now[0])
        if len(calls) == 1:
            return first
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(weather, "fetch_weather", fetch)
    cache = weather.WeatherCache(1.0, 2.0, refresh_interval=600)

    assert cache.get() is first
    now[0] = 1600.0
    assert cache.get() is first
    assert cache.age_seconds == 600.0
    assert calls == [1000.0, 1600.0]
