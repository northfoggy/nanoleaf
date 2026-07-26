import requests

from nanoleaf_ctl import weather


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
