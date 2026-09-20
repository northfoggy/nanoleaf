"""Weather and solar-radiation data from Open-Meteo.

The simulator prefers irradiance incident on the configured vertical window
because it already incorporates solar angle, window orientation, and clouds.
Cloud cover remains available for color rendering and as a compatibility
fallback when irradiance is missing.
"""

import math
import time
from dataclasses import dataclass

import requests

# Open-Meteo WMO weather codes → simplified condition
# https://open-meteo.com/en/docs
_WMO_CONDITIONS = {
    0: "clear",
    1: "mostly_clear",
    2: "partly_cloudy",
    3: "overcast",
    45: "fog",
    48: "fog",
    51: "drizzle",
    53: "drizzle",
    55: "drizzle",
    56: "drizzle",
    57: "drizzle",
    61: "rain",
    63: "rain",
    65: "rain_heavy",
    66: "rain",
    67: "rain_heavy",
    71: "snow",
    73: "snow",
    75: "snow_heavy",
    77: "snow",
    80: "rain",
    81: "rain",
    82: "rain_heavy",
    85: "snow",
    86: "snow_heavy",
    95: "thunderstorm",
    96: "thunderstorm",
    99: "thunderstorm",
}


@dataclass
class WeatherState:
    """Current weather conditions relevant to window light."""
    cloud_cover: int          # 0-100%
    condition: str            # clear, partly_cloudy, overcast, rain, etc.
    is_day: bool
    timestamp: float          # when this was fetched
    irradiance_w_m2: float | None = None
    shortwave_radiation_w_m2: float | None = None
    direct_radiation_w_m2: float | None = None
    diffuse_radiation_w_m2: float | None = None
    irradiance_orientation: str | None = None
    source: str = "open-meteo"

    @property
    def cloud_factor(self) -> float:
        """0.0 = clear sky, 1.0 = fully overcast.

        Used to modulate brightness and color of the window light.
        """
        return self.cloud_cover / 100.0

    @property
    def is_rainy(self) -> bool:
        return self.condition in ("drizzle", "rain", "rain_heavy", "thunderstorm")

    @property
    def is_stormy(self) -> bool:
        return self.condition in ("rain_heavy", "thunderstorm")


_OPEN_METEO_AZIMUTH = {
    # Open-Meteo: 0° south, -90° east, +90° west, ±180° north.
    "north": 180,
    "northeast": -135,
    "east": -90,
    "southeast": -45,
    "south": 0,
    "southwest": 45,
    "west": 90,
    "northwest": 135,
}


def _orientation_azimuth(facing: str) -> int:
    """Translate a compass-facing window to Open-Meteo's azimuth system."""
    return _OPEN_METEO_AZIMUTH.get(facing.lower(), 0)


def _optional_radiation(current: dict, name: str) -> float | None:
    """Return a valid non-negative radiation value or ``None``."""
    value = current.get(name)
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def fetch_weather(lat: float, lon: float, facing: str = "southwest") -> WeatherState:
    """Fetch current weather and vertical-window irradiance from Open-Meteo."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": (
            "cloud_cover,weather_code,is_day,shortwave_radiation,"
            "direct_radiation,diffuse_radiation,global_tilted_irradiance"
        ),
        "tilt": 90,  # a window is a vertical plane
        "azimuth": _orientation_azimuth(facing),
        "timezone": "UTC",
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    current = data["current"]
    weather_code = current.get("weather_code", 0)

    return WeatherState(
        cloud_cover=current.get("cloud_cover", 0),
        condition=_WMO_CONDITIONS.get(weather_code, "clear"),
        is_day=bool(current.get("is_day", 1)),
        timestamp=time.time(),
        irradiance_w_m2=_optional_radiation(current, "global_tilted_irradiance"),
        shortwave_radiation_w_m2=_optional_radiation(current, "shortwave_radiation"),
        direct_radiation_w_m2=_optional_radiation(current, "direct_radiation"),
        diffuse_radiation_w_m2=_optional_radiation(current, "diffuse_radiation"),
        irradiance_orientation=facing.lower(),
    )


class WeatherCache:
    """Caches weather data so we don't hit the API every 60 seconds.

    Fetches fresh data every `refresh_interval` seconds (default 10 min).
    If a fetch fails, keeps using the last known good data.
    """

    def __init__(
        self, lat: float, lon: float, facing: str = "southwest",
        refresh_interval: int = 600,
    ):
        self.lat = lat
        self.lon = lon
        self.facing = facing
        self.refresh_interval = refresh_interval
        self._cached: WeatherState | None = None
        self._last_fetch: float = 0
        self._last_attempt: float = 0

    def get(self) -> WeatherState | None:
        """Get current weather, fetching if stale."""
        now = time.time()
        if now - self._last_attempt >= self.refresh_interval:
            self._last_attempt = now
            try:
                self._cached = fetch_weather(self.lat, self.lon, self.facing)
                self._last_fetch = now
            except (requests.RequestException, OSError, ValueError, KeyError):
                # Network down or API issue — use cached data
                pass
        return self._cached

    @property
    def age_seconds(self) -> float | None:
        """Age of the last successful observation, or None before first success."""
        if self._cached is None:
            return None
        return max(0.0, time.time() - self._cached.timestamp)
