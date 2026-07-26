"""Nanoleaf client — thin wrapper around nanoleafapi with config persistence.

Handles discovery, authentication, and provides a unified interface
for the CLI layer.
"""

from nanoleafapi import Nanoleaf, discovery

from nanoleaf_ctl import config


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
    nl = Nanoleaf(ip)
    token = nl.auth_token
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
        nl = Nanoleaf(ip)
        config.save_device(ip, nl.auth_token)

    return nl


def get_info(nl: Nanoleaf) -> dict:
    """Get device info as a flat summary dict."""
    info = nl.get_info()
    return {
        "name": info.get("name", "Unknown"),
        "model": info.get("model", "Unknown"),
        "serial": info.get("serialNo", "Unknown"),
        "firmware": info.get("firmwareVersion", "Unknown"),
        "num_panels": info.get("panelLayout", {}).get("layout", {}).get("numPanels", "?"),
        "power": nl.get_power(),
        "brightness": nl.get_brightness(),
        "effect": nl.get_current_effect(),
    }


def set_color_from_string(nl: Nanoleaf, color_str: str) -> None:
    """Parse a color string and apply it.

    Accepts:
      - Hex: "#ff0000" or "ff0000"
      - RGB: "255,0,0" or "255 0 0"
      - Named: "red", "blue", "green", etc.
    """
    color_str = color_str.strip().lower()

    # Named colors
    named = {
        "red": (255, 0, 0),
        "green": (0, 255, 0),
        "blue": (0, 0, 255),
        "white": (255, 255, 255),
        "orange": (255, 165, 0),
        "yellow": (255, 255, 0),
        "cyan": (0, 255, 255),
        "magenta": (255, 0, 255),
        "purple": (128, 0, 128),
        "pink": (255, 105, 180),
        "warm": (255, 180, 100),
        "cool": (100, 180, 255),
    }

    if color_str in named:
        nl.set_color(named[color_str])
        return

    # Hex color
    if color_str.startswith("#"):
        color_str = color_str[1:]
    if len(color_str) == 6:
        try:
            r = int(color_str[0:2], 16)
            g = int(color_str[2:4], 16)
            b = int(color_str[4:6], 16)
            nl.set_color((r, g, b))
            return
        except ValueError:
            pass

    # RGB comma or space separated
    for sep in [",", " "]:
        if sep in color_str:
            parts = [p.strip() for p in color_str.split(sep) if p.strip()]
            if len(parts) == 3:
                try:
                    r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
                    nl.set_color((r, g, b))
                    return
                except ValueError:
                    pass

    raise ValueError(
        f"Cannot parse color '{color_str}'. "
        "Use hex (#ff0000), RGB (255,0,0), or a name (red, blue, etc.)"
    )
