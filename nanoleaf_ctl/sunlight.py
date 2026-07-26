"""Window light simulator for Nanoleaf Skylights.

Instead of simulating the sun directly, this simulates what light
actually looks like coming *through a window* — with real colors
(blue hour is blue, golden hour is orange), diffused brightness,
and window-orientation awareness.

Uses the astral library for real solar positions based on your
latitude/longitude, so the simulation matches what's actually
happening outside your window.
"""

import colorsys
import time
from datetime import datetime, date, timedelta, timezone
from dataclasses import dataclass

import requests
from astral import LocationInfo
from astral.sun import sun, noon as solar_noon, elevation, azimuth
from nanoleafapi import Nanoleaf
from zoneinfo import ZoneInfo

DEFAULT_LAT = 34.13
DEFAULT_LON = -84.34
DEFAULT_TZ = "America/New_York"
DEFAULT_FACING = "southwest"
DEFAULT_PEAK = 75


@dataclass
class WindowConfig:
    """Configuration for the window simulation."""
    latitude: float = DEFAULT_LAT
    longitude: float = DEFAULT_LON
    timezone: str = DEFAULT_TZ
    facing: str = DEFAULT_FACING
    peak_brightness: int = DEFAULT_PEAK
    night_off: bool = True        # turn off at night vs dim warm glow
    brightness_bias: int = 0      # -50 to +50, added to computed brightness


# Maps sun elevation angle to how the sky looks through a window.
# Elevation < 0 = sun below horizon.
#
# These RGB values represent the *color cast* of light coming through
# a window at each solar phase, not raw sunlight color.
#
# The key insight: we use set_color (RGB) for colorful periods
# (sunrise, sunset, blue hour) and set_color_temp (Kelvin) for the
# neutral midday period when window light is essentially white.

def _make_location(cfg: WindowConfig) -> LocationInfo:
    return LocationInfo(
        name="home",
        region="",
        timezone=cfg.timezone,
        latitude=cfg.latitude,
        longitude=cfg.longitude,
    )


def _sun_times(cfg: WindowConfig, dt: date | None = None) -> dict:
    """Get sun event times for the configured location.

    The astral ``sun()`` helper computes dawn, sunrise, noon, sunset and
    dusk in one call.  If *any* single event is undefined for the given
    date/location (e.g. no dusk during polar summer), the whole call
    raises ``ValueError``.  We only need ``noon`` (and occasionally
    ``sunrise``), so fall back to computing them individually.
    """
    loc = _make_location(cfg)
    if dt is None:
        dt = date.today()
    try:
        return sun(loc.observer, date=dt)
    except ValueError:
        return {"noon": solar_noon(loc.observer, date=dt)}


def _solar_elevation(cfg: WindowConfig, dt: datetime | None = None) -> float:
    loc = _make_location(cfg)
    if dt is None:
        dt = datetime.now(timezone.utc)
    return elevation(loc.observer, dt)


def _solar_azimuth(cfg: WindowConfig, dt: datetime | None = None) -> float:
    loc = _make_location(cfg)
    if dt is None:
        dt = datetime.now(timezone.utc)
    return azimuth(loc.observer, dt)


def _facing_factor(cfg: WindowConfig, solar_az: float) -> float:
    """How much direct light the window gets based on its orientation.

    Returns 0.0 (sun behind the wall) to 1.0 (sun directly facing window).
    A window always gets some ambient/diffuse light even when the sun
    is on the other side — that's modeled by the base ambient.
    """
    facing_az = {
        "north": 0,
        "northeast": 45,
        "east": 90,
        "southeast": 135,
        "south": 180,
        "southwest": 225,
        "west": 270,
        "northwest": 315,
    }.get(cfg.facing.lower(), 180)

    # Angular difference between sun and window facing direction
    diff = abs(solar_az - facing_az)
    if diff > 180:
        diff = 360 - diff

    # Window has ~180 degree field of view
    # Direct: diff < 45°  → strong light (1.0)
    # Angled: 45-90°      → moderate (0.5-1.0)
    # Oblique: 90-135°    → weak indirect (0.1-0.5)
    # Behind: >135°       → ambient only (0.1)
    if diff <= 45:
        return 1.0
    elif diff <= 90:
        return 1.0 - 0.5 * ((diff - 45) / 45)
    elif diff <= 135:
        return 0.5 - 0.4 * ((diff - 90) / 45)
    else:
        return 0.1


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


def _lerp_rgb(c1: tuple, c2: tuple, t: float) -> tuple[int, int, int]:
    return (
        round(_lerp(c1[0], c2[0], t)),
        round(_lerp(c1[1], c2[1], t)),
        round(_lerp(c1[2], c2[2], t)),
    )


def compute_window_light(
    cfg: WindowConfig,
    dt: datetime | None = None,
) -> dict:
    """Compute what the light should look like right now.

    Returns a dict with:
      mode: "color" or "color_temp"
      rgb: (r, g, b) if mode is "color"
      color_temp: int if mode is "color_temp"
      brightness: 0-100
      phase: human-readable phase name
    """
    if dt is None:
        dt = datetime.now(timezone.utc)

    elev = _solar_elevation(cfg, dt)
    az = _solar_azimuth(cfg, dt)
    facing = _facing_factor(cfg, az)
    local_date = dt.astimezone(ZoneInfo(cfg.timezone)).date()
    times = _sun_times(cfg, local_date)

    # Determine solar phase and base light properties
    # Elevation ranges:
    #   < -18°   astronomical night (dark)
    #   -18 to -12°  astronomical twilight
    #   -12 to -6°   nautical twilight (deep blue)
    #   -6 to -4°    civil twilight / blue hour
    #   -4 to 0°     blue-golden transition
    #   0 to 6°      golden hour / sunrise-sunset
    #   6 to 15°     low sun, warm
    #   15 to 40°    mid sun, neutral
    #   > 40°        high sun, cool white

    if elev < -18:
        # Full night
        result = {
            "mode": "off" if cfg.night_off else "color",
            "rgb": (20, 15, 10),
            "color_temp": 1200,
            "brightness": 0 if cfg.night_off else 2,
            "phase": "night",
        }

    elif elev < -6:
        # Twilight → deep blue through window
        # -18 to -6: blend from dark to deep blue
        t = (elev - (-18)) / 12.0  # 0 at -18°, 1 at -6°
        rgb = _lerp_rgb((5, 5, 15), (30, 50, 120), t)
        br = round(_lerp(0, 8, t) * facing + _lerp(0, 5, t))
        result = {
            "mode": "color",
            "rgb": rgb,
            "brightness": min(br, cfg.peak_brightness),
            "phase": "twilight",
        }

    elif elev < 0:
        # Blue hour — the sky through a window is a rich deep blue
        # with hints of purple/pink near horizon
        t = (elev - (-6)) / 6.0  # 0 at -6°, 1 at 0°

        # Are we in morning or evening? Check if before or after noon
        noon_time = times["noon"]
        if dt < noon_time:
            # Morning blue hour → transitioning toward warm
            rgb = _lerp_rgb((30, 50, 120), (120, 80, 60), t)
        else:
            # Evening blue hour → coming from warm
            rgb = _lerp_rgb((120, 80, 60), (30, 50, 120), 1 - t)

        br = round(_lerp(8, 25, t) * (0.3 + 0.7 * facing))
        result = {
            "mode": "color",
            "rgb": rgb,
            "brightness": min(max(br, 3), cfg.peak_brightness),
            "phase": "blue hour",
        }

    elif elev < 6:
        # Golden hour — warm orange/amber light flooding through window
        t = elev / 6.0  # 0 at horizon, 1 at 6°
        noon_time = times["noon"]

        if dt < noon_time:
            # Morning golden hour: salmon → warm white
            rgb_start = (255, 130, 50)   # rich sunrise orange
            rgb_end = (255, 200, 130)    # warm morning glow
        else:
            # Evening golden hour: warm → deep orange
            rgb_start = (255, 200, 130)
            rgb_end = (255, 130, 50)
            t = 1 - t

        rgb = _lerp_rgb(rgb_start, rgb_end, t)
        br = round(_lerp(25, 55, t) * (0.4 + 0.6 * facing))
        result = {
            "mode": "color",
            "rgb": rgb,
            "brightness": min(max(br, 10), cfg.peak_brightness),
            "phase": "golden hour",
        }

    elif elev < 15:
        # Low sun — transitioning from golden to neutral white
        # Use color temp here as the colorful part fades
        t = (elev - 6) / 9.0  # 0 at 6°, 1 at 15°
        ct = round(_lerp(2700, 4000, t))
        br = round(_lerp(55, 70, t) * (0.5 + 0.5 * facing))
        result = {
            "mode": "color_temp",
            "color_temp": ct,
            "brightness": min(max(br, 30), cfg.peak_brightness),
            "phase": "morning" if dt < times["noon"] else "afternoon",
        }

    elif elev < 40:
        # Mid sun — good neutral daylight through window
        t = (elev - 15) / 25.0
        ct = round(_lerp(4000, 5500, t))
        br = round(_lerp(70, cfg.peak_brightness, t) * (0.6 + 0.4 * facing))
        result = {
            "mode": "color_temp",
            "color_temp": ct,
            "brightness": min(max(br, 40), cfg.peak_brightness),
            "phase": "day",
        }

    else:
        # High sun — bright, slightly cool daylight
        ct = 5500
        br = round(cfg.peak_brightness * (0.7 + 0.3 * facing))
        result = {
            "mode": "color_temp",
            "color_temp": ct,
            "brightness": min(br, cfg.peak_brightness),
            "phase": "midday",
        }

    # Apply brightness bias (keeps it in 0-100 range)
    if cfg.brightness_bias and result["mode"] != "off":
        result["brightness"] = max(0, min(100, result["brightness"] + cfg.brightness_bias))

    return result


def apply_weather(state: dict, weather) -> dict:
    """Modulate a light state based on current weather conditions.

    Effects of weather on window light:
    - Overcast: dims brightness, shifts color cooler/greyer
    - Rain: further dims, adds a blue-grey cast
    - Heavy rain/storms: significant dimming, flat grey light
    - Fog: dims, very flat and diffuse
    - Clouds mute golden hour colors (no vivid sunset on a cloudy day)
    """
    if weather is None:
        return state

    state = dict(state)  # don't mutate the original
    if state["mode"] == "off" or state.get("brightness", 0) == 0:
        state["weather"] = weather.condition
        state["cloud_cover"] = weather.cloud_cover
        state["weather_timestamp"] = weather.timestamp
        return state
    cloud = weather.cloud_factor  # 0.0 clear → 1.0 overcast
    condition = weather.condition

    # Brightness reduction from clouds
    # Clear: no change. Overcast: -40%. Rain: -55%. Storm: -65%.
    if condition in ("rain_heavy", "thunderstorm"):
        br_factor = 1.0 - 0.65 * cloud
    elif weather.is_rainy:
        br_factor = 1.0 - 0.55 * cloud
    elif condition == "fog":
        br_factor = 1.0 - 0.50 * cloud
    else:
        br_factor = 1.0 - 0.40 * cloud

    state["brightness"] = max(2, round(state["brightness"] * br_factor))

    # Color shifts
    if state["mode"] == "color":
        r, g, b = state["rgb"]
        # Clouds desaturate and shift cool (add blue/grey)
        # Heavy clouds → blend toward grey
        grey = round((r + g + b) / 3)
        overcast_rgb = (
            round(grey * 0.85),   # slightly cool grey
            round(grey * 0.90),
            round(grey * 1.05),   # blue tint
        )
        state["rgb"] = _lerp_rgb((r, g, b), overcast_rgb, cloud * 0.7)

    elif state["mode"] == "color_temp":
        # Overcast light is cooler and flatter
        # Push color temp up (cooler) on cloudy days
        ct = state["color_temp"]
        ct_shift = round(500 * cloud)  # up to +500K cooler
        state["color_temp"] = min(6500, ct + ct_shift)

    # Add weather info to state for logging
    state["weather"] = condition
    state["cloud_cover"] = weather.cloud_cover
    state["weather_timestamp"] = weather.timestamp

    return state


_API_TIMEOUT = 10  # seconds; prevents silent hangs on unresponsive device


def _checked_response(response):
    """Raise on HTTP errors and return the response for optional decoding."""
    response.raise_for_status()
    return response


def apply_light(nl: Nanoleaf, state: dict, transition: int = 0) -> None:
    """Apply a computed light state to the Nanoleaf.

    transition: fade duration in seconds (0 = instant).  Uses the
    nanoleafapi library methods for maximum compatibility.

    All HTTP calls use a timeout so a hung device can't block the
    simulator thread forever.
    """
    mode = state["mode"]
    br = state["brightness"]

    if mode == "off" or br == 0:
        _checked_response(requests.put(
            nl.url + "/state", json={"on": {"value": False}}, timeout=_API_TIMEOUT,
        ))
        return

    power = _checked_response(
        requests.get(nl.url + "/state/on", timeout=_API_TIMEOUT)
    ).json()
    if not power.get("value", False):
        _checked_response(requests.put(
            nl.url + "/state", json={"on": {"value": True}}, timeout=_API_TIMEOUT,
        ))

    if mode == "color":
        r, g, b = state["rgb"]
        h, s, _v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        # Send hue, saturation, and target brightness in one atomic PUT
        # to avoid the flash caused by set_color() temporarily setting
        # brightness to the V component before set_brightness() corrects it.
        data = {
            "hue": {"value": int(h * 360)},
            "sat": {"value": int(s * 100)},
            "brightness": {"value": br, "duration": transition},
        }
        _checked_response(requests.put(nl.url + "/state", json=data, timeout=_API_TIMEOUT))
    elif mode == "color_temp":
        data = {
            "ct": {"value": state["color_temp"]},
            "brightness": {"value": br, "duration": transition},
        }
        _checked_response(requests.put(nl.url + "/state", json=data, timeout=_API_TIMEOUT))


def preview_day(cfg: WindowConfig) -> list[dict]:
    """Generate a preview of the full day's light states, one per 30 minutes."""
    tz = ZoneInfo(cfg.timezone)
    today = datetime.now(tz).date()

    results = []
    for half_hour in range(48):
        h = half_hour // 2
        m = (half_hour % 2) * 30
        dt = datetime(today.year, today.month, today.day, h, m, tzinfo=tz)
        state = compute_window_light(cfg, dt)
        state["time"] = f"{h:02d}:{m:02d}"
        results.append(state)
    return results


def run_simulator(
    nl: Nanoleaf,
    cfg: WindowConfig,
    update_interval: int = 60,
    weather_cache=None,
    on_update=None,
    on_disconnect=None,
    on_reconnect=None,
):
    """Run the window light simulator continuously.

    Resilient to the light switch being turned off and on:
    - When the device is unreachable, keeps polling every cycle
    - When it comes back, re-applies the correct state immediately
    """
    last_state = None
    last_applied_brightness = None
    device_online = True

    while True:
        now = datetime.now(timezone.utc)
        state = compute_window_light(cfg, now)

        # Apply weather modulation if available
        if weather_cache is not None:
            weather = weather_cache.get()
            state = apply_weather(state, weather)

        # Conflict detection: if another controller changed the brightness, warn
        if last_applied_brightness is not None and device_online:
            try:
                actual_br = _checked_response(
                    requests.get(nl.url + "/state/brightness", timeout=_API_TIMEOUT)
                ).json().get("value", 0)
                if abs(actual_br - last_applied_brightness) > 3 and on_update:
                    on_update({"phase": "CONFLICT", "mode": "warning", "brightness": actual_br,
                               "conflict_detail": f"device={actual_br}% vs last_set={last_applied_brightness}%"})
            except (requests.RequestException, OSError, ValueError) as e:
                device_online = False
                last_state = None
                last_applied_brightness = None
                if on_disconnect:
                    on_disconnect(str(e))

        state_key = (state["mode"], state.get("rgb"), state.get("color_temp"), state["brightness"])
        try:
            # If device was offline or state changed, apply
            if not device_online or state_key != last_state:
                apply_light(nl, state, transition=update_interval)
                last_applied_brightness = state["brightness"]
                last_state = state_key

                if not device_online:
                    device_online = True
                    if on_reconnect:
                        on_reconnect(state)
                elif on_update:
                    on_update(state)

        except (requests.RequestException, OSError) as e:
            # Device unreachable — switch probably turned off
            if device_online:
                device_online = False
                last_state = None  # force re-apply when it comes back
                last_applied_brightness = None
                if on_disconnect:
                    on_disconnect(str(e))

        time.sleep(update_interval)
