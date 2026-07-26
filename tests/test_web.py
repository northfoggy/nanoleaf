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
    monkeypatch.setattr(web, "_auto_start_simulator", lambda: events.append("auto-start"))
    monkeypatch.setattr(web, "_start_watchdog", lambda: events.append("watchdog"))
    monkeypatch.setattr(web, "_sd_notify", lambda state: events.append(state))

    web.run(host="127.0.0.1", port=5000)

    assert events == [
        "bound", "auto-start", "watchdog", "READY=1", "serve", "STOPPING=1", "close",
    ]
