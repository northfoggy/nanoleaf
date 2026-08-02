import os

import pytest
import requests

from nanoleaf_ctl import web


def test_window_config_validation():
    cfg = web._build_window_config({"lat": 34.13, "lon": -84.34, "peak": 75})
    assert cfg.facing == "southwest"

    with pytest.raises(ValueError):
        web._build_window_config({"lat": 91})
    with pytest.raises(ValueError):
        web._build_window_config({"tz": "Not/A_Timezone"})


def test_start_connection_failure_does_not_acquire_process_lock(monkeypatch):
    acquired = []
    monkeypatch.setattr(web, "_sim_running", False)
    monkeypatch.setattr(web, "_get_nl", lambda: (_ for _ in ()).throw(ConnectionError("offline")))
    monkeypatch.setattr(web.config, "acquire_sunlight_lock", lambda: acquired.append(True))

    response = web.app.test_client().post("/api/sunlight/start", json={})

    assert response.status_code == 502
    assert acquired == []


def test_run_notifies_ready_only_after_server_is_bound(monkeypatch):
    events = []

    class Server:
        def serve_forever(self):
            events.append("serve")

        def server_close(self):
            events.append("close")

    def make_server(*args, **kwargs):
        events.append("bound")
        return Server()

    monkeypatch.setattr(web, "make_server", make_server)
    monkeypatch.setattr(
        web, "_start_auto_start_worker", lambda: events.append("auto-start-worker")
    )
    monkeypatch.setattr(web, "_start_watchdog", lambda: events.append("watchdog"))
    monkeypatch.setattr(web, "_sd_notify", lambda state: events.append(state))

    web.run(host="127.0.0.1", port=5000)

    assert events == [
        "bound", "watchdog", "READY=1", "auto-start-worker", "serve", "STOPPING=1", "close",
    ]


def test_redact_removes_token_from_urls_and_messages():
    secret = "this-is-the-device-secret"
    text = (
        f"failed at http://10.0.0.2:16021/api/v1/{secret}/state "
        f"and /api/v1/{secret}/state auth_token={secret}"
    )

    redacted = web._redact(text)

    assert secret not in redacted
    assert redacted.count("[REDACTED]") == 3


def test_existing_log_scrub_is_bounded_and_redacted(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "_LOG_MAX_BYTES", 1024)
    log_path = tmp_path / "sunlight.log"
    secret = "this-is-the-device-secret"
    log_path.write_text(
        ("old entry\n" * 300)
        + f"http://device/api/v1/{secret}/state failed\n",
        encoding="utf-8",
    )

    web._scrub_existing_log(str(log_path))

    scrubbed = log_path.read_text(encoding="utf-8")
    assert log_path.stat().st_size <= 1024
    assert secret not in scrubbed
    assert "[REDACTED]" in scrubbed


def test_log_family_scrubs_rotated_relative_urls(tmp_path):
    secret = "this-is-the-device-secret"
    log_path = tmp_path / "sunlight.log"
    for suffix in ("", ".1", ".2", ".3"):
        (tmp_path / f"sunlight.log{suffix}").write_text(
            f"request failed for /api/v1/{secret}/state\n",
            encoding="utf-8",
        )

    web._scrub_log_family(str(log_path))

    for suffix in ("", ".1", ".2", ".3"):
        scrubbed = (tmp_path / f"sunlight.log{suffix}").read_text(encoding="utf-8")
        assert secret not in scrubbed
        assert "/api/v1/[REDACTED]/state" in scrubbed
        if os.name != "nt":
            assert (tmp_path / f"sunlight.log{suffix}").stat().st_mode & 0o777 == 0o600


def test_api_failure_does_not_return_transport_details(monkeypatch):
    secret = "this-is-the-device-secret"
    monkeypatch.setattr(
        web,
        "_get_nl",
        lambda: (_ for _ in ()).throw(
            ConnectionError(f"http://device/api/v1/{secret}/state refused")
        ),
    )

    response = web.app.test_client().get("/api/info")

    assert response.status_code == 502
    assert secret not in response.get_data(as_text=True)
    assert "Unable to read device status" in response.get_data(as_text=True)


def test_direct_control_starts_override_and_resume_clears_it(monkeypatch):
    monkeypatch.setattr(web, "_sim_running", True)
    monkeypatch.setattr(web, "_control_mode", "automation")
    monkeypatch.setattr(web, "_manual_override_until", None)
    monkeypatch.setattr(web, "_nap_brightness", None)
    monkeypatch.setattr(web, "_device_online", True)
    monkeypatch.setattr(web, "_get_nl", lambda: object())
    monkeypatch.setattr(web, "_device_put", lambda *_: None)

    client = web.app.test_client()
    changed = client.post("/api/brightness", json={"level": 42})
    assert changed.status_code == 200
    assert web._control_mode == "manual_override"
    assert web._manual_override_until is not None

    resumed = client.post("/api/sunlight/resume", json={})
    assert resumed.status_code == 200
    assert web._control_mode == "automation"
    assert web._manual_override_until is None
    assert web._nap_brightness is None
    assert web._device_online is False


def test_nap_mode_applies_amber_scene_and_reports_status(monkeypatch):
    applied = []
    monkeypatch.setattr(web, "_sim_running", True)
    monkeypatch.setattr(web, "_control_mode", "automation")
    monkeypatch.setattr(web, "_manual_override_until", None)
    monkeypatch.setattr(web, "_nap_brightness", None)
    monkeypatch.setattr(web, "_device_online", True)
    monkeypatch.setattr(web, "_get_nl", lambda: object())
    monkeypatch.setattr(web.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(
        web.sunlight,
        "apply_light",
        lambda nl, state, transition: applied.append((nl, state, transition)),
    )

    client = web.app.test_client()
    response = client.post("/api/nap/start", json={"minutes": 40, "brightness": 5})

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "nap started", "minutes": 40, "brightness": 5, "until": 3_400.0,
    }
    assert applied[0][1] == {"mode": "color", "rgb": (255, 106, 0), "brightness": 5}
    assert applied[0][2] == 5
    assert web._control_mode == "nap"
    status = client.get("/api/sunlight/status").get_json()
    assert status["nap"] == {
        "until": 3_400.0, "brightness": 5, "rgb": [255, 106, 0],
    }


def test_nap_mode_requires_running_automation(monkeypatch):
    monkeypatch.setattr(web, "_sim_running", False)

    response = web.app.test_client().post("/api/nap/start", json={})

    assert response.status_code == 409
    assert "must be running" in response.get_json()["error"]


def test_nap_mode_can_end_early(monkeypatch):
    monkeypatch.setattr(web, "_sim_running", True)
    monkeypatch.setattr(web, "_control_mode", "nap")
    monkeypatch.setattr(web, "_manual_override_until", 3_400.0)
    monkeypatch.setattr(web, "_nap_brightness", 5)
    monkeypatch.setattr(web, "_device_online", True)

    response = web.app.test_client().post("/api/nap/stop", json={})

    assert response.status_code == 200
    assert web._control_mode == "automation"
    assert web._manual_override_until is None
    assert web._nap_brightness is None
    assert web._device_online is False


def test_nap_mode_expiry_resumes_automation(monkeypatch):
    messages = []
    monkeypatch.setattr(web, "_control_mode", "nap")
    monkeypatch.setattr(web, "_manual_override_until", 3_400.0)
    monkeypatch.setattr(web, "_nap_brightness", 5)
    monkeypatch.setattr(web, "_log", messages.append)

    assert web._timed_override_active(now=3_400.0) is False
    assert web._control_mode == "automation"
    assert web._manual_override_until is None
    assert web._nap_brightness is None
    assert messages == ["Nap mode complete; resuming automation"]


def test_manual_override_pauses_demo_without_applying(monkeypatch):
    applied = []
    sleeps = []
    monkeypatch.setattr(web, "_sim_running", True)
    monkeypatch.setattr(web, "_sim_generation", 17)
    monkeypatch.setattr(web, "_control_mode", "manual_override")
    monkeypatch.setattr(web, "_manual_override_until", web.time.time() + 3600)
    monkeypatch.setattr(web, "_device_online", True)
    monkeypatch.setattr(
        web.sunlight,
        "_sun_times",
        lambda cfg: {"sunrise": None},
    )
    monkeypatch.setattr(
        web.sunlight,
        "compute_window_light",
        lambda cfg, now: {
            "phase": "daylight",
            "mode": "color_temp",
            "color_temp": 5000,
            "brightness": 50,
        },
    )
    monkeypatch.setattr(
        web.sunlight,
        "apply_light",
        lambda *args, **kwargs: applied.append((args, kwargs)),
    )

    def stop_after_pause(seconds):
        sleeps.append(seconds)
        monkeypatch.setattr(web, "_sim_running", False)

    monkeypatch.setattr(web.time, "sleep", stop_after_pause)

    web._run_sim_loop_inner(
        object(),
        web.sunlight.WindowConfig(),
        weather_cache=None,
        my_generation=17,
        demo=True,
    )

    assert applied == []
    assert sleeps == [5]


def test_health_endpoint_reports_process_and_device_state(monkeypatch):
    monkeypatch.setattr(web, "_sim_running", True)
    monkeypatch.setattr(web, "_device_online", False)

    response = web.app.test_client().get("/api/health")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok", "simulator_running": True, "device_online": False,
    }


def test_dashboard_includes_branding_and_dynamic_house_scene():
    response = web.app.test_client().get("/")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "NanoLeaf Sunlight Simulator" in html
    assert "by Quicksilver Industries LTD." in html
    assert "not affiliated with" in html
    assert "support.nanoleaf.me" in html
    assert 'id="houseGraphic"' in html
    assert 'id="sceneLocation"' in html
    assert 'id="sceneOrientation"' in html
    assert 'id="sceneWeather"' in html
    assert 'id="napMinutes"' in html
    assert 'id="napBrightness"' in html
    assert 'id="napStartBtn"' in html
