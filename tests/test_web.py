import os
import threading

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


def test_auto_start_retries_until_rebooting_device_is_available(monkeypatch):
    attempts = []
    waits = []
    threads = []
    messages = []

    class CancelEvent:
        def is_set(self):
            return False

        def wait(self, seconds):
            waits.append(seconds)
            return False

    class Thread:
        def __init__(self, **kwargs):
            threads.append(kwargs)

        def start(self):
            threads[-1]["started"] = True

    def connect():
        attempts.append(True)
        if len(attempts) < 3:
            raise ConnectionError("device still booting")
        return object()

    monkeypatch.setattr(web, "_sim_running", False)
    monkeypatch.setattr(web, "_sim_generation", 0)
    monkeypatch.setattr(web, "_auto_start_cancel", CancelEvent())
    monkeypatch.setattr(web, "_get_nl", connect)
    monkeypatch.setattr(web, "_setup_file_logging", lambda: None)
    monkeypatch.setattr(web._file_logger, "warning", lambda *args: messages.append(args))
    monkeypatch.setattr(web._file_logger, "info", lambda *args: messages.append(args))
    monkeypatch.setattr(web.WeatherCache, "__init__", lambda self, *args: None)
    monkeypatch.setattr(web.config, "acquire_sunlight_lock", lambda: object())
    monkeypatch.setattr(web.threading, "Thread", Thread)

    web._auto_start_simulator()

    assert len(attempts) == 3
    assert waits == [5, 10]
    assert web._sim_running is True
    assert threads[0]["started"] is True
    assert any("Auto-start attempt" in args[0] for args in messages)


def test_auto_start_retry_can_be_canceled(monkeypatch):
    acquired = []

    class CancelEvent:
        def is_set(self):
            return False

        def wait(self, _seconds):
            return True

    monkeypatch.setattr(web, "_sim_running", False)
    monkeypatch.setattr(web, "_auto_start_cancel", CancelEvent())
    monkeypatch.setattr(
        web, "_get_nl",
        lambda: (_ for _ in ()).throw(ConnectionError("offline")),
    )
    monkeypatch.setattr(web, "_setup_file_logging", lambda: None)
    monkeypatch.setattr(web._file_logger, "warning", lambda *args: None)
    monkeypatch.setattr(web._file_logger, "info", lambda *args: None)
    monkeypatch.setattr(web.WeatherCache, "__init__", lambda self, *args: None)
    monkeypatch.setattr(
        web.config, "acquire_sunlight_lock", lambda: acquired.append(True),
    )

    web._auto_start_simulator()

    assert acquired == []


def test_auto_start_rechecks_cancellation_after_connection(monkeypatch):
    cancel = threading.Event()
    acquired = []

    def connect_then_cancel():
        cancel.set()
        return object()

    monkeypatch.setattr(web, "_sim_running", False)
    monkeypatch.setattr(web, "_auto_start_cancel", cancel)
    monkeypatch.setattr(web, "_get_nl", connect_then_cancel)
    monkeypatch.setattr(web, "_setup_file_logging", lambda: None)
    monkeypatch.setattr(web._file_logger, "info", lambda *args: None)
    monkeypatch.setattr(web.WeatherCache, "__init__", lambda self, *args: None)
    monkeypatch.setattr(
        web.config, "acquire_sunlight_lock", lambda: acquired.append(True),
    )

    web._auto_start_simulator()

    assert acquired == []
    assert web._sim_running is False


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
    def apply_nap(nl, state, transition):
        assert web._control_mode == "nap"
        applied.append((nl, state, transition))

    monkeypatch.setattr(web.sunlight, "apply_light", apply_nap)

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


def test_nap_mode_rolls_back_reservation_when_device_write_fails(monkeypatch):
    monkeypatch.setattr(web, "_sim_running", True)
    monkeypatch.setattr(web, "_control_mode", "manual_override")
    monkeypatch.setattr(web, "_manual_override_until", 9_999.0)
    monkeypatch.setattr(web, "_nap_brightness", None)
    monkeypatch.setattr(web, "_device_online", True)
    monkeypatch.setattr(web, "_get_nl", lambda: object())
    monkeypatch.setattr(web.time, "time", lambda: 1_000.0)

    def fail_after_reservation(*_args, **_kwargs):
        assert web._control_mode == "nap"
        raise requests.ConnectionError("device unavailable")

    monkeypatch.setattr(web.sunlight, "apply_light", fail_after_reservation)

    response = web.app.test_client().post("/api/nap/start", json={})

    assert response.status_code == 502
    assert web._control_mode == "manual_override"
    assert web._manual_override_until == 9_999.0
    assert web._nap_brightness is None
    assert web._device_online is False


def test_automation_rechecks_override_after_waiting_for_write_lock(monkeypatch):
    applied = []
    first_check_complete = threading.Event()
    write_lock = threading.Lock()
    real_update = web._update_timed_override
    update_calls = 0

    def tracked_update(now=None):
        nonlocal update_calls
        update_calls += 1
        result = real_update(now)
        if update_calls == 1:
            first_check_complete.set()
        return result

    monkeypatch.setattr(web, "_device_write_lock", write_lock)
    monkeypatch.setattr(web, "_update_timed_override", tracked_update)
    monkeypatch.setattr(web, "_sim_running", True)
    monkeypatch.setattr(web, "_sim_generation", 29)
    monkeypatch.setattr(web, "_control_mode", "automation")
    monkeypatch.setattr(web, "_manual_override_until", None)
    monkeypatch.setattr(web, "_nap_brightness", None)
    monkeypatch.setattr(web, "_device_online", False)
    monkeypatch.setattr(
        web.sunlight,
        "compute_window_light",
        lambda *_: {
            "phase": "daylight", "mode": "color_temp",
            "color_temp": 5000, "brightness": 50,
        },
    )
    monkeypatch.setattr(
        web.sunlight, "apply_light",
        lambda *_args, **_kwargs: applied.append(True),
    )
    monkeypatch.setattr(
        web.time, "sleep",
        lambda _seconds: monkeypatch.setattr(web, "_sim_running", False),
    )

    write_lock.acquire()
    worker = threading.Thread(target=web._run_sim_loop_inner, args=(
        object(), web.sunlight.WindowConfig(), None, 29, False,
    ))
    worker.start()
    assert first_check_complete.wait(timeout=1)
    monkeypatch.setattr(web, "_control_mode", "nap")
    monkeypatch.setattr(web, "_manual_override_until", web.time.time() + 60)
    monkeypatch.setattr(web, "_nap_brightness", 5)
    write_lock.release()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert update_calls >= 2
    assert applied == []


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

    assert web._update_timed_override(now=3_400.0) == (False, True)
    assert web._control_mode == "automation"
    assert web._manual_override_until is None
    assert web._nap_brightness is None
    assert messages == ["Nap mode complete; resuming automation"]


def test_expiry_reapplies_unchanged_automatic_target(monkeypatch):
    applied = []
    sleeps = []
    monkeypatch.setattr(web, "_sim_running", True)
    monkeypatch.setattr(web, "_sim_generation", 23)
    monkeypatch.setattr(web, "_control_mode", "automation")
    monkeypatch.setattr(web, "_manual_override_until", None)
    monkeypatch.setattr(web, "_nap_brightness", None)
    monkeypatch.setattr(web, "_device_online", True)
    monkeypatch.setattr(web, "_device_get", lambda *_: {"value": 50})
    monkeypatch.setattr(
        web.sunlight,
        "compute_window_light",
        lambda *_: {
            "phase": "daylight", "mode": "color_temp",
            "color_temp": 5000, "brightness": 50,
        },
    )

    def apply_light(*_args, **_kwargs):
        applied.append(True)
        if len(applied) == 2:
            monkeypatch.setattr(web, "_sim_running", False)

    def advance_sleep(_seconds):
        sleeps.append(True)
        if len(sleeps) == 1:
            monkeypatch.setattr(web, "_control_mode", "nap")
            monkeypatch.setattr(web, "_manual_override_until", 0.0)
            monkeypatch.setattr(web, "_nap_brightness", 5)
        if len(sleeps) > 120:
            monkeypatch.setattr(web, "_sim_running", False)

    monkeypatch.setattr(web.sunlight, "apply_light", apply_light)
    monkeypatch.setattr(web.time, "sleep", advance_sleep)

    web._run_sim_loop_inner(
        object(), web.sunlight.WindowConfig(), weather_cache=None,
        my_generation=23, demo=False,
    )

    assert len(applied) == 2


@pytest.mark.parametrize(
    ("computed_state", "actual_on"),
    [
        ({
            "phase": "daylight", "mode": "color_temp",
            "color_temp": 5000, "brightness": 50,
        }, False),
        ({
            "phase": "night", "mode": "off", "brightness": 0,
        }, True),
    ],
)
def test_external_power_change_reapplies_unchanged_automatic_target(
    monkeypatch, computed_state, actual_on,
):
    applied = []
    messages = []
    device_reads = []
    monkeypatch.setattr(web, "_sim_running", True)
    monkeypatch.setattr(web, "_sim_generation", 37)
    monkeypatch.setattr(web, "_control_mode", "automation")
    monkeypatch.setattr(web, "_manual_override_until", None)
    monkeypatch.setattr(web, "_nap_brightness", None)
    monkeypatch.setattr(web, "_device_online", True)
    monkeypatch.setattr(
        web.sunlight, "compute_window_light", lambda *_: computed_state.copy(),
    )

    def read_device(_nl, path):
        device_reads.append(path)
        if path == "/state/on":
            return {"value": actual_on}
        return {"value": computed_state["brightness"]}

    def apply_light(*_args, **_kwargs):
        applied.append(True)
        if len(applied) == 2:
            monkeypatch.setattr(web, "_sim_running", False)

    monkeypatch.setattr(web, "_device_get", read_device)
    monkeypatch.setattr(web.sunlight, "apply_light", apply_light)
    monkeypatch.setattr(web, "_log", messages.append)
    monkeypatch.setattr(web.time, "sleep", lambda _seconds: None)

    web._run_sim_loop_inner(
        object(), web.sunlight.WindowConfig(), weather_cache=None,
        my_generation=37, demo=False,
    )

    assert applied == [True, True]
    assert device_reads == ["/state/on"]
    assert any("device power" in message and "reapplying target" in message
               for message in messages)


def test_canceling_override_breaks_sleep_and_reapplies_immediately(monkeypatch):
    applied = []
    sleeps = []
    monkeypatch.setattr(web, "_sim_running", True)
    monkeypatch.setattr(web, "_sim_generation", 31)
    monkeypatch.setattr(web, "_control_mode", "manual_override")
    monkeypatch.setattr(web, "_manual_override_until", web.time.time() + 3_600)
    monkeypatch.setattr(web, "_nap_brightness", None)
    monkeypatch.setattr(web, "_device_online", True)
    monkeypatch.setattr(
        web.sunlight,
        "compute_window_light",
        lambda *_: {
            "phase": "daylight", "mode": "color_temp",
            "color_temp": 5000, "brightness": 50,
        },
    )

    def apply_light(*_args, **_kwargs):
        applied.append(True)
        monkeypatch.setattr(web, "_sim_running", False)

    def cancel_during_sleep(_seconds):
        sleeps.append(True)
        monkeypatch.setattr(web, "_control_mode", "automation")
        monkeypatch.setattr(web, "_manual_override_until", None)

    monkeypatch.setattr(web.sunlight, "apply_light", apply_light)
    monkeypatch.setattr(web.time, "sleep", cancel_during_sleep)

    web._run_sim_loop_inner(
        object(), web.sunlight.WindowConfig(), weather_cache=None,
        my_generation=31, demo=False,
    )

    assert applied == [True]
    assert len(sleeps) == 1


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
