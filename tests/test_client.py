import requests
import pytest

from nanoleaf_ctl import client


class _Device:
    url = "http://device.test/api/v1/token"


class _FailedResponse:
    def raise_for_status(self):
        raise requests.HTTPError("device rejected request")


def test_rgb_components_must_be_in_range():
    assert client.parse_color("12,34,56") == (12, 34, 56)
    with pytest.raises(ValueError):
        client.parse_color("999,0,0")


def test_color_write_raises_on_http_failure(monkeypatch):
    monkeypatch.setattr(client.requests, "put", lambda *args, **kwargs: _FailedResponse())

    with pytest.raises(requests.HTTPError):
        client.set_color_from_string(_Device(), "red")


def test_black_color_turns_device_off(monkeypatch):
    calls = []
    monkeypatch.setattr(
        client,
        "_put",
        lambda nl, path, payload=None: calls.append((nl, path, payload)),
    )

    device = _Device()
    client.set_color_from_string(device, "#000000")

    assert calls == [(device, "/state", {"on": {"value": False}})]


def test_pair_uses_timeout_and_saves_token(monkeypatch):
    calls = {}

    class Response:
        def raise_for_status(self):
            calls["checked"] = True

        def json(self):
            return {"auth_token": "paired-token"}

    def post(url, timeout):
        calls.update(url=url, timeout=timeout)
        return Response()

    monkeypatch.setattr(client.requests, "post", post)
    monkeypatch.setattr(client.config, "save_device", lambda ip, token: calls.update(ip=ip, token=token))

    assert client.pair("192.0.2.20") == "paired-token"
    assert calls["timeout"] == client._API_TIMEOUT
    assert calls["checked"] is True
    assert calls["token"] == "paired-token"
