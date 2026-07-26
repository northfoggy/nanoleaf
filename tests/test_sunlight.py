from datetime import date, datetime, timezone

import pytest
import requests

from nanoleaf_ctl import sunlight


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
