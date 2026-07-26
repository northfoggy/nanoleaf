"""Nanoleaf client — thin wrapper around nanoleafapi with config persistence.

Handles discovery, authentication, and provides a unified interface
for the CLI layer.
"""

import colorsys

import requests
from nanoleafapi import Nanoleaf, discovery

from nanoleaf_ctl import config

_API_TIMEOUT = 10


def discover_devices(timeout: int = 5) -> list[str]:
    """Scan the local network for Nanoleaf devices via SSDP.

    Returns a list of IP addresses found.
    """
    results = discovery.discover_devices(timeout=timeout)
    if results is None:
        return []
    # discovery returns dict {name: ip} or list depending on version
    if isinstance(results, dict):
        return list(results.values())
    return list(results)


def pair(ip: str) -> str:
    """Pair with a Nanoleaf device and save the auth token.

    The device must be in pairing mode (hold power button 5-7 seconds).
    Returns the auth token on success.
    """
    response = requests.post(
        f"http://{ip}:16021/api/v1/new", timeout=_API_TIMEOUT,
    )
    response.raise_for_status()
    token = response.json()["auth_token"]
    config.save_device(ip, token)
    return token


def connect(ip: str | None = None) -> Nanoleaf:
    """Connect to a Nanoleaf device.

    If ip is not provided, uses the saved config.
    If an auth token is saved for the IP, it will be reused.
    """
    saved_ip, saved_token = config.get_device()

    if ip is None:
        ip = saved_ip
    if ip is None:
        raise ConnectionError(
            "No device IP provided and none saved. "
            "Run 'nanoleaf-ctl discover' or 'nanoleaf-ctl pair <ip>' first."
        )

    # If we have a saved token for this IP, use it
    if saved_token and (saved_ip == ip):
        nl = Nanoleaf(ip, auth_token=saved_token)
    else:
        token = pair(ip)
        nl = Nanoleaf(ip, auth_token=token)

    return nl


def get_info(nl: Nanoleaf) -> dict:
    """Get device info as a flat summary dict."""
    response = requests.get(nl.url, timeout=_API_TIMEOUT)
    response.raise_for_status()
    info = response.json()
    state = info.get("state", {})
    return {
        "name": info.get("name", "Unknown"),
        "model": info.get("model", "Unknown"),
        "serial": info.get("serialNo", "Unknown"),
        "firmware": info.get("firmwareVersion", "Unknown"),
        "num_panels": info.get("panelLayout", {}).get("layout", {}).get("numPanels", "?"),
        "power": state.get("on", {}).get("value", False),
        "brightness": state.get("brightness", {}).get("value", 0),
        "effect": info.get("effects", {}).get("select", ""),
    }


def _get_json(nl: Nanoleaf, path: str = ""):
    response = requests.get(nl.url + path, timeout=_API_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _put(nl: Nanoleaf, path: str, payload: dict | None = None) -> None:
    response = requests.put(nl.url + path, json=payload, timeout=_API_TIMEOUT)
    response.raise_for_status()


def get_power(nl: Nanoleaf) -> bool:
    return bool(_get_json(nl, "/state/on").get("value", False))


def set_power(nl: Nanoleaf, value: bool) -> None:
    _put(nl, "/state", {"on": {"value": value}})


def toggle_power(nl: Nanoleaf) -> bool:
    value = not get_power(nl)
    set_power(nl, value)
    return value


def set_brightness(nl: Nanoleaf, level: int) -> None:
    _put(nl, "/state", {"brightness": {"value": level}})


def set_color_temp(nl: Nanoleaf, temperature: int) -> None:
    _put(nl, "/state", {"ct": {"value": temperature}})


def list_effects(nl: Nanoleaf) -> list[str]:
    return _get_json(nl, "/effects/effectsList")


def get_current_effect(nl: Nanoleaf) -> str:
    return _get_json(nl, "/effects/select")


def set_effect(nl: Nanoleaf, name: str) -> None:
    _put(nl, "/effects", {"select": name})


def identify(nl: Nanoleaf) -> None:
    _put(nl, "/identify")


NAMED_COLORS = {
    "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255),
    "white": (255, 255, 255), "orange": (255, 165, 0), "yellow": (255, 255, 0),
    "cyan": (0, 255, 255), "magenta": (255, 0, 255), "purple": (128, 0, 128),
    "pink": (255, 105, 180), "warm": (255, 180, 100), "cool": (100, 180, 255),
}


def parse_color(color_str: str) -> tuple[int, int, int]:
    """Parse a color string to (r, g, b). Raises ValueError on bad input."""
    s = color_str.strip().lower()
    if s in NAMED_COLORS:
        return NAMED_COLORS[s]
    if s.startswith("#"):
        s = s[1:]
    if len(s) == 6:
        try:
            return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        except ValueError:
            pass
    for sep in (",", " "):
        if sep in s:
            parts = [p.strip() for p in s.split(sep) if p.strip()]
            if len(parts) == 3:
                try:
                    rgb = int(parts[0]), int(parts[1]), int(parts[2])
                    if all(0 <= component <= 255 for component in rgb):
                        return rgb
                except ValueError:
                    pass
    raise ValueError(
        f"Cannot parse color '{color_str}'. "
        "Use hex (#ff0000), RGB (255,0,0), or a name (red, blue, etc.)"
    )


def set_color_from_string(nl: Nanoleaf, color_str: str) -> None:
    """Parse a color string and apply it via direct API (no brightness flash)."""
    r, g, b = parse_color(color_str)
    h, s, _v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    response = requests.put(
        nl.url + "/state",
        json={"hue": {"value": int(h * 360)}, "sat": {"value": int(s * 100)}},
        timeout=_API_TIMEOUT,
    )
    response.raise_for_status()
