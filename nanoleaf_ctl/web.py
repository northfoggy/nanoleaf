"""Web interface for nanoleaf-ctl.

Provides a browser-based dashboard for controlling Nanoleaf panels
and monitoring the sunlight simulator. Runs on the local network
so you can control everything from your phone.
"""

import logging
from logging.handlers import RotatingFileHandler
import os
import re
import socket
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import requests as _requests
from flask import Flask, request, jsonify, Response
from werkzeug.serving import make_server
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nanoleaf_ctl import client, config, sunlight
from nanoleaf_ctl.weather import WeatherCache

# systemd watchdog support (sd_notify)
def _sd_notify(state: str) -> None:
    """Send a notification to systemd if running under it."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    import socket as _sock
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with _sock.socket(_sock.AF_UNIX, _sock.SOCK_DGRAM) as s:
            s.sendto(state.encode(), addr)
    except OSError:
        pass


# ── Shared state for the sunlight simulator thread ──────────────────

_sim_lock = threading.Lock()
_device_write_lock = threading.Lock()
_sim_thread: threading.Thread | None = None
_sim_state: dict | None = None        # latest computed light state
_sim_config: sunlight.WindowConfig | None = None
_sim_running = False
_sim_demo = False                     # True when running in time-lapse demo mode
_sim_generation = 0                   # incremented on each start; old threads check & exit
_sim_file_lock = None                 # OS file lock to prevent duplicates
_device_online = True
_device_last_seen: float | None = None
_last_device_error: str | None = None
_control_mode = "automation"
_manual_override_until: float | None = None
_nap_brightness: int | None = None
_sim_log: deque = deque(maxlen=200)
_watchdog_stop = threading.Event()
_watchdog_thread: threading.Thread | None = None


_file_logger = logging.getLogger("nanoleaf.sunlight")
_file_logger.setLevel(logging.DEBUG)
_log_handler = None
_log_setup_lock = threading.Lock()
_LOG_MAX_BYTES = 1_000_000
_NAP_DEFAULT_MINUTES = 40
_NAP_DEFAULT_BRIGHTNESS = 5
_NAP_RGB = (255, 106, 0)


def _scrub_existing_log(log_path: str) -> None:
    """Redact and bound an existing log without loading it all into memory."""
    if not os.path.exists(log_path):
        return
    temp_path = f"{log_path}.scrub"
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as source:
            if size > _LOG_MAX_BYTES:
                source.seek(-_LOG_MAX_BYTES, os.SEEK_END)
                source.readline()  # Discard a partial first line.
            existing = source.read(_LOG_MAX_BYTES)
        redacted = _redact(existing.decode("utf-8", errors="replace"))
        with open(temp_path, "w", encoding="utf-8", newline="") as target:
            target.write(redacted)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, log_path)
    except OSError:
        try:
            os.remove(temp_path)
        except OSError:
            pass


def _scrub_log_family(log_path: str) -> None:
    """Scrub the active simulator log and each bounded rotation."""
    for suffix in ("", ".1", ".2", ".3"):
        _scrub_existing_log(f"{log_path}{suffix}")


def _setup_file_logging() -> None:
    """Set up persistent file logging so crashes are traceable."""
    global _log_handler
    with _log_setup_lock:
        if _log_handler is not None:
            return
        log_dir = os.path.expanduser("~/.nanoleaf-ctl")
        os.makedirs(log_dir, mode=0o700, exist_ok=True)
        os.chmod(log_dir, 0o700)
        log_path = os.path.join(log_dir, "sunlight.log")
        _scrub_log_family(log_path)
        _log_handler = RotatingFileHandler(
            log_path,
            maxBytes=_LOG_MAX_BYTES,
            backupCount=3,
        )
        os.chmod(log_path, 0o600)
        _log_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
        ))
        _file_logger.addHandler(_log_handler)


def _redact(text: object) -> str:
    """Remove Nanoleaf credentials and credential-bearing URLs from text."""
    value = str(text)
    value = re.sub(
        r"((?:https?://[^\s]+?)?/api/v1/)[^/\s?'\"()]+",
        r"\1[REDACTED]",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"(?i)(auth(?:entication)?[_ -]?token\s*[=:]\s*)[^\s,;]+",
        r"\1[REDACTED]",
        value,
    )


def _log(msg: str) -> None:
    msg = _redact(msg)
    ts = datetime.now().strftime("%H:%M:%S")
    _sim_log.append(f"[{ts}] {msg}")
    _file_logger.info(msg)


def _api_failure(exc: Exception, operation: str):
    """Log useful diagnostics without returning device details to the browser."""
    _log(f"{operation} failed: {type(exc).__name__}: {exc}")
    return jsonify({"error": f"Unable to {operation}; check device connectivity"}), 502


def _begin_manual_override(minutes: int = 60) -> None:
    """Pause automation after a person directly changes the lights."""
    global _control_mode, _manual_override_until, _nap_brightness
    with _sim_lock:
        if _sim_running:
            _control_mode = "manual_override"
            _manual_override_until = time.time() + minutes * 60
            _nap_brightness = None
            _log(f"Manual override started for {minutes} minutes")


def _update_timed_override(now: float | None = None) -> tuple[bool, bool]:
    """Expire timed control and return ``(active, expired_this_call)``."""
    global _control_mode, _manual_override_until, _nap_brightness
    expired_mode = None
    current_time = time.time() if now is None else now
    with _sim_lock:
        timed_mode = _control_mode in ("manual_override", "nap")
        if (timed_mode and _manual_override_until is not None
                and current_time >= _manual_override_until):
            expired_mode = _control_mode
            _control_mode = "automation"
            _manual_override_until = None
            _nap_brightness = None
            timed_mode = False
    if expired_mode == "nap":
        _log("Nap mode complete; resuming automation")
    elif expired_mode == "manual_override":
        _log("Manual override expired; resuming automation")
    return timed_mode, expired_mode is not None


def _run_sim_loop(nl, cfg, weather_cache, my_generation, demo=False):
    """Background loop identical to sunlight.run_simulator but updating shared state.

    In demo mode, cycles through a full 24h day in ~8 minutes:
    each tick advances simulated time by 15 minutes, with 5s real intervals.
    """
    global _sim_state, _sim_running, _sim_demo, _sim_file_lock, _device_online

    _setup_file_logging()
    try:
        _run_sim_loop_inner(nl, cfg, weather_cache, my_generation, demo)
    except BaseException as e:
        _log(f"FATAL: {type(e).__name__}: {e}")
        import traceback
        _log(traceback.format_exc())
        with _sim_lock:
            _sim_running = False
            _sim_demo = False

    with _sim_lock:
        if _sim_generation == my_generation:
            lock_fd = _sim_file_lock
            _sim_file_lock = None
        else:
            lock_fd = None

    if lock_fd is not None:
        config.release_sunlight_lock(lock_fd)
        _log("Simulator stopped")
    else:
        _log(f"Simulator gen={my_generation} exiting (superseded by gen={_sim_generation})")


def _run_sim_loop_inner(nl, cfg, weather_cache, my_generation, demo=False):
    global _sim_state, _sim_running, _sim_file_lock, _device_online
    global _device_last_seen, _last_device_error

    hostname = socket.gethostname()
    last_state_key = None
    last_applied_brightness = None
    mode_label = "DEMO" if demo else "Simulator"
    _log(f"{mode_label} started (gen={my_generation}, host={hostname}, pid={__import__('os').getpid()}) — facing={cfg.facing}, lat={cfg.latitude}, lon={cfg.longitude}, peak={cfg.peak_brightness}")
    _log("Device connection initialized")

    # Demo mode: start 1 hour before sunrise, advance 15 min per tick
    if demo:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(cfg.timezone)
        s = sunlight._sun_times(cfg)
        sunrise = s.get("sunrise")
        if sunrise is not None:
            sunrise = sunrise.astimezone(tz)
            demo_time = (sunrise - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        else:
            # Sunrise unavailable (polar day/night edge case) — start at 5 AM
            from datetime import date as _date
            demo_time = datetime(_date.today().year, _date.today().month, _date.today().day, 5, 0, tzinfo=tz)
            sunrise = demo_time  # for log message
        demo_end = demo_time + timedelta(hours=24)
        _log(f"Demo: starting at {demo_time.strftime('%H:%M')} (1h before sunrise {sunrise.strftime('%H:%M')})")

    while _sim_running and _sim_generation == my_generation:
        if demo:
            now = demo_time.astimezone(timezone.utc)
            _log(f"Demo time: {demo_time.strftime('%H:%M')}")
        else:
            now = datetime.now(timezone.utc)

        state = sunlight.compute_window_light(cfg, now)
        _log(f"Computed: phase={state['phase']}, mode={state['mode']}, "
             f"br={state['brightness']}, "
             f"{'rgb=' + str(state.get('rgb')) if state['mode'] == 'color' else 'ct=' + str(state.get('color_temp'))}")

        if weather_cache is not None:
            weather = weather_cache.get()
            if weather:
                _log(f"Weather: {weather.condition}, cloud={weather.cloud_cover}%")
            state = sunlight.apply_weather(state, weather)
            state["weather_age_seconds"] = weather_cache.age_seconds

        state_key = (state["mode"], state.get("rgb"), state.get("color_temp"), state["brightness"])

        # Conflict detection: read back device brightness and compare to what we last set
        if not demo and last_applied_brightness is not None and _device_online:
            try:
                actual_br = _device_get(nl, "/state/brightness").get("value", 0)
                with _sim_lock:
                    _device_last_seen = time.time()
                if abs(actual_br - last_applied_brightness) > 3:
                    _log(f"CONFLICT: device brightness is {actual_br}% but we last set {last_applied_brightness}% — another controller is likely active!")
            except (_requests.RequestException, OSError, ValueError) as exc:
                with _sim_lock:
                    _device_online = False
                    _last_device_error = _redact(exc)
                last_state_key = None
                last_applied_brightness = None
                _log(f"Device probe failed: {type(exc).__name__}")

        with _sim_lock:
            _sim_state = state

        override_active, override_expired = _update_timed_override()
        if override_expired:
            # The device still shows the override scene. Invalidate both
            # cached values even when the newly computed target is unchanged.
            last_state_key = None
            last_applied_brightness = None

        applied = False
        try:
            if override_active:
                _log(f"{_control_mode.replace('_', ' ').title()} active, skipping automation apply")
            elif not _device_online or state_key != last_state_key:
                with _device_write_lock:
                    # Nap Mode can be reserved while this loop waits for the
                    # write lock. Recheck before touching the device.
                    override_active, expired_while_waiting = _update_timed_override()
                    if expired_while_waiting:
                        last_state_key = None
                        last_applied_brightness = None
                    if override_active:
                        _log("Timed override reserved, skipping automation apply")
                    else:
                        _log(f"Applying: {state['mode']} br={state['brightness']} "
                             f"{'rgb=' + str(state.get('rgb')) if state['mode'] == 'color' else 'ct=' + str(state.get('color_temp'))}")
                        transition = 5 if demo else 60
                        sunlight.apply_light(nl, state, transition=transition)
                        _log("Applied OK")
                        last_applied_brightness = state["brightness"]
                        last_state_key = state_key
                        applied = True
                        with _sim_lock:
                            if not _device_online:
                                _log("Device reconnected")
                            _device_online = True
                            _device_last_seen = time.time()
                            _last_device_error = None
            else:
                _log("No change, skipping apply")
        except (_requests.RequestException, OSError) as e:
            _log(f"ERROR applying light: {type(e).__name__}: {e}")
            with _sim_lock:
                _device_online = False
                _last_device_error = _redact(e)
                last_state_key = None
                last_applied_brightness = None

        if demo and override_active:
            # Pause the demo clock as well as device writes. Otherwise a demo
            # would race through the simulated day while manual control is
            # active and finish before the user resumes it.
            time.sleep(5)
            continue

        if demo:
            demo_time += timedelta(minutes=15)
            # Stop after a full day cycle (24h from start)
            if demo_time >= demo_end:
                _log("Demo: full day cycle complete")
                with _sim_lock:
                    _sim_running = False
                    _sim_demo = False
                break
            # Skip sleep when state didn't change — blast through night hours
            # and only pause on visible transitions
            if not applied:
                continue
            sleep_secs = 5
        else:
            sleep_secs = 60

        _log(f"Sleeping {sleep_secs}s")
        # Sleep in short intervals so we can exit promptly on stop/supersede
        for _ in range(sleep_secs):
            if not _sim_running or _sim_generation != my_generation:
                break
            if override_active:
                still_active, expired_during_sleep = _update_timed_override()
                if expired_during_sleep or not still_active:
                    last_state_key = None
                    last_applied_brightness = None
                    break
                override_active = still_active
            time.sleep(1)


# ── Flask app ───────────────────────────────────────────────────────

app = Flask(__name__)
_nl = None
_nl_lock = threading.Lock()


def _get_nl():
    global _nl
    with _nl_lock:
        if _nl is None:
            _nl = client.connect()
        return _nl



def _device_put(nl, payload: dict) -> None:
    """PUT to device state with timeout protection."""
    response = _requests.put(nl.url + "/state", json=payload, timeout=sunlight._API_TIMEOUT)
    response.raise_for_status()


def _device_get(nl, path: str = "") -> dict:
    """GET from device with timeout protection."""
    url = nl.url + path if path else nl.url
    response = _requests.get(url, timeout=sunlight._API_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _device_effects(nl) -> list[str]:
    response = _requests.get(
        nl.url + "/effects/effectsList", timeout=sunlight._API_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _build_window_config(data: dict) -> sunlight.WindowConfig:
    """Validate dashboard input and return a simulator configuration."""
    latitude = float(data.get("lat", sunlight.DEFAULT_LAT))
    longitude = float(data.get("lon", sunlight.DEFAULT_LON))
    peak = int(data.get("peak", sunlight.DEFAULT_PEAK))
    bias = int(data.get("bias", 0))
    timezone_name = str(data.get("tz", sunlight.DEFAULT_TZ))
    facing = str(data.get("facing", sunlight.DEFAULT_FACING)).lower()

    if not -90 <= latitude <= 90:
        raise ValueError("Latitude must be between -90 and 90")
    if not -180 <= longitude <= 180:
        raise ValueError("Longitude must be between -180 and 180")
    if not 1 <= peak <= 100:
        raise ValueError("Peak brightness must be between 1 and 100")
    if not -50 <= bias <= 50:
        raise ValueError("Brightness bias must be between -50 and 50")
    if facing not in {
        "north", "northeast", "east", "southeast",
        "south", "southwest", "west", "northwest",
    }:
        raise ValueError("Invalid window orientation")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone '{timezone_name}'") from exc

    return sunlight.WindowConfig(
        latitude=latitude,
        longitude=longitude,
        timezone=timezone_name,
        facing=facing,
        peak_brightness=peak,
        night_off=not bool(data.get("night_glow", False)),
        brightness_bias=bias,
    )


# ── API routes ──────────────────────────────────────────────────────

@app.route("/")
def index():
    return Response(_HTML, content_type="text/html")


@app.route("/api/info")
def api_info():
    try:
        nl = _get_nl()
        info = _device_get(nl)
        state = info.get("state", {})
        on = state.get("on", {}).get("value", False)
        br = state.get("brightness", {}).get("value", 0)
        ct = state.get("ct", {}).get("value", 0)
        return jsonify({
            "name": info.get("name", "Unknown"),
            "model": info.get("model", "Unknown"),
            "serial": info.get("serialNo", "Unknown"),
            "firmware": info.get("firmwareVersion", "Unknown"),
            "num_panels": info.get("panelLayout", {}).get("layout", {}).get("numPanels", "?"),
            "power": on,
            "brightness": br,
            "color_temp": ct,
            "effect": info.get("effects", {}).get("select", ""),
        })
    except Exception as e:
        return _api_failure(e, "read device status")


@app.route("/api/power", methods=["POST"])
def api_power():
    try:
        nl = _get_nl()
        action = request.json.get("action", "toggle")
        if action == "on":
            _device_put(nl, {"on": {"value": True}})
        elif action == "off":
            _device_put(nl, {"on": {"value": False}})
        else:
            current = _device_get(nl, "/state/on").get("value", False)
            _device_put(nl, {"on": {"value": not current}})
        power = _device_get(nl, "/state/on").get("value", False)
        _begin_manual_override()
        return jsonify({"power": "on" if power else "off"})
    except Exception as e:
        return _api_failure(e, "change power")


@app.route("/api/brightness", methods=["POST"])
def api_brightness():
    try:
        nl = _get_nl()
        level = request.json.get("level", 50)
        level = max(0, min(100, int(level)))
        _device_put(nl, {"brightness": {"value": level}})
        _begin_manual_override()
        return jsonify({"brightness": level})
    except Exception as e:
        return _api_failure(e, "change brightness")


@app.route("/api/color", methods=["POST"])
def api_color():
    try:
        nl = _get_nl()
        color_str = request.json.get("color", "white")
        client.set_color_from_string(nl, color_str)
        _begin_manual_override()
        return jsonify({"color": color_str})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return _api_failure(e, "change color")


@app.route("/api/color-temp", methods=["POST"])
def api_color_temp():
    try:
        nl = _get_nl()
        temp = request.json.get("temp", 4000)
        temp = max(1200, min(6500, int(temp)))
        _device_put(nl, {"ct": {"value": temp}})
        _begin_manual_override()
        return jsonify({"color_temp": temp})
    except Exception as e:
        return _api_failure(e, "change color temperature")


@app.route("/api/effects")
def api_effects():
    try:
        nl = _get_nl()
        effects = _device_effects(nl)
        current = _device_get(nl, "/effects/select")
        return jsonify({"effects": sorted(effects), "current": current})
    except Exception as e:
        return _api_failure(e, "load effects")


@app.route("/api/effect", methods=["POST"])
def api_effect():
    try:
        nl = _get_nl()
        name = request.json.get("name", "")
        effects = _device_effects(nl)
        if name not in effects:
            return jsonify({"error": f"Effect '{name}' not found"}), 404
        response = _requests.put(
            nl.url + "/effects", json={"select": name}, timeout=sunlight._API_TIMEOUT,
        )
        response.raise_for_status()
        _begin_manual_override()
        return jsonify({"effect": name})
    except Exception as e:
        return _api_failure(e, "change effect")


@app.route("/api/nap/start", methods=["POST"])
def api_nap_start():
    """Dim to a warm amber scene, then automatically resume automation."""
    global _control_mode, _manual_override_until, _nap_brightness
    global _device_online, _device_last_seen, _last_device_error
    data = request.get_json(silent=True) or {}
    try:
        duration = max(5, min(180, int(data.get("minutes", _NAP_DEFAULT_MINUTES))))
        brightness = max(1, min(20, int(data.get("brightness", _NAP_DEFAULT_BRIGHTNESS))))
    except (TypeError, ValueError):
        return jsonify({"error": "Nap minutes and brightness must be integers"}), 400

    with _sim_lock:
        if not _sim_running:
            return jsonify({
                "status": "error",
                "error": "Sunlight automation must be running so Nap Mode can restore it",
            }), 409

    try:
        nl = _get_nl()
    except Exception as e:
        return _api_failure(e, "start Nap Mode")

    with _device_write_lock:
        until = time.time() + duration * 60
        with _sim_lock:
            if not _sim_running:
                return jsonify({
                    "status": "error",
                    "error": "Sunlight automation must be running so Nap Mode can restore it",
                }), 409
            previous_control = (
                _control_mode, _manual_override_until, _nap_brightness,
            )
            # Reserve the mode before the device write. The simulator uses the
            # same write lock and rechecks this state after acquiring it.
            _control_mode = "nap"
            _manual_override_until = until
            _nap_brightness = brightness

        try:
            sunlight.apply_light(nl, {
                "mode": "color",
                "rgb": _NAP_RGB,
                "brightness": brightness,
            }, transition=5)
        except Exception as e:
            with _sim_lock:
                if _control_mode == "nap" and _manual_override_until == until:
                    _control_mode, _manual_override_until, _nap_brightness = previous_control
                _device_online = False
                _last_device_error = _redact(e)
            return _api_failure(e, "start Nap Mode")

        with _sim_lock:
            if _control_mode != "nap" or _manual_override_until != until:
                return jsonify({
                    "status": "error",
                    "error": "Nap Mode setup was superseded by another control request",
                }), 409
            _device_online = True
            _device_last_seen = time.time()
            _last_device_error = None
    _log(f"Nap mode started for {duration} minutes at {brightness}% brightness")
    return jsonify({
        "status": "nap started",
        "minutes": duration,
        "brightness": brightness,
        "until": until,
    })


@app.route("/api/nap/stop", methods=["POST"])
def api_nap_stop():
    """End Nap Mode early and force sunlight automation to reconcile."""
    global _control_mode, _manual_override_until, _nap_brightness, _device_online
    with _sim_lock:
        if not _sim_running:
            return jsonify({"status": "error", "error": "Simulator is not running"}), 409
        if _control_mode != "nap":
            return jsonify({"status": "not active"})
        _control_mode = "automation"
        _manual_override_until = None
        _nap_brightness = None
        _device_online = False
    _log("Nap mode ended early; automation resume requested")
    return jsonify({"status": "resuming"})


@app.route("/api/sunlight/status")
def api_sunlight_status():
    with _sim_lock:
        running = _sim_running
        demo = _sim_demo
        state = dict(_sim_state) if _sim_state else None
        online = _device_online
        last_seen = _device_last_seen
        control_mode = _control_mode
        override_until = _manual_override_until
        nap_brightness = _nap_brightness
        cfg = _sim_config
    result = {
        "running": running,
        "demo": demo,
        "device_online": online,
        "control_mode": control_mode if running else "stopped",
        "manual_override_until": override_until,
        "nap": ({
            "until": override_until,
            "brightness": nap_brightness,
            "rgb": list(_NAP_RGB),
        } if control_mode == "nap" and override_until is not None else None),
        "device_last_seen": last_seen,
    }
    if state:
        result["state"] = state
    if cfg:
        result["config"] = {
            "latitude": cfg.latitude,
            "longitude": cfg.longitude,
            "timezone": cfg.timezone,
            "facing": cfg.facing,
            "peak_brightness": cfg.peak_brightness,
            "brightness_bias": cfg.brightness_bias,
        }
    return jsonify(result)


@app.route("/api/sunlight/start", methods=["POST"])
def api_sunlight_start():
    global _sim_thread, _sim_config, _sim_running, _sim_demo, _sim_generation, _sim_file_lock
    global _control_mode, _manual_override_until, _nap_brightness

    with _sim_lock:
        if _sim_running:
            _log("Start requested but already running")
            return jsonify({"status": "already running"})

    data = request.get_json(silent=True) or {}
    demo = bool(data.get("demo", False))
    try:
        cfg = _build_window_config(data)
    except (TypeError, ValueError) as exc:
        return jsonify({"status": "error", "error": str(exc)}), 400

    try:
        nl = _get_nl()
    except Exception as exc:
        return _api_failure(exc, "connect to device")

    weather_cache = None
    if not demo and not data.get("no_weather", False):
        weather_cache = WeatherCache(cfg.latitude, cfg.longitude)

    # Recheck after potentially slow setup, then claim both locks atomically.
    with _sim_lock:
        if _sim_running:
            _log("Start requested but already running")
            return jsonify({"status": "already running"})

        _sim_file_lock = config.acquire_sunlight_lock()
        if _sim_file_lock is None:
            holder = config.read_lock_info() or "unknown"
            _log(f"Cannot start: lock held by {holder}")
            return jsonify({"status": "error", "error": f"Another sunlight instance is already running (held by {holder}). Stop it first."}), 409

        _sim_log.clear()
        _log(f"Start request: {data}")
        _sim_config = cfg
        _sim_running = True
        _sim_demo = demo
        _control_mode = "automation"
        _manual_override_until = None
        _nap_brightness = None
        _sim_generation += 1
        gen = _sim_generation

    _sim_thread = threading.Thread(
        target=_run_sim_loop, args=(nl, cfg, weather_cache, gen, demo), daemon=True,
    )
    _sim_thread.start()
    return jsonify({"status": "demo started" if demo else "started"})


@app.route("/api/sunlight/stop", methods=["POST"])
def api_sunlight_stop():
    global _sim_running, _sim_demo, _control_mode, _manual_override_until, _nap_brightness
    with _sim_lock:
        _sim_running = False
        _sim_demo = False
        _control_mode = "stopped"
        _manual_override_until = None
        _nap_brightness = None
    # Lock is released in _run_sim_loop when it exits
    _log("Stop requested")
    return jsonify({"status": "stopped"})


@app.route("/api/sunlight/resume", methods=["POST"])
def api_sunlight_resume():
    """End a manual override and force automation to reconcile next cycle."""
    global _control_mode, _manual_override_until, _nap_brightness, _device_online
    with _sim_lock:
        if not _sim_running:
            return jsonify({"status": "error", "error": "Simulator is not running"}), 409
        _control_mode = "automation"
        _manual_override_until = None
        _nap_brightness = None
        _device_online = False
    _log("Manual override ended; automation resume requested")
    return jsonify({"status": "resuming"})


@app.route("/api/health")
def api_health():
    with _sim_lock:
        return jsonify({
            "status": "ok",
            "simulator_running": _sim_running,
            "device_online": _device_online,
        })


@app.route("/api/sunlight/log")
def api_sunlight_log():
    return jsonify({"lines": list(_sim_log)})


@app.route("/api/sunlight/preview")
def api_sunlight_preview():
    try:
        cfg = _build_window_config({
            "lat": request.args.get("lat", sunlight.DEFAULT_LAT),
            "lon": request.args.get("lon", sunlight.DEFAULT_LON),
            "tz": request.args.get("tz", sunlight.DEFAULT_TZ),
            "facing": request.args.get("facing", sunlight.DEFAULT_FACING),
            "peak": request.args.get("peak", sunlight.DEFAULT_PEAK),
            "bias": request.args.get("bias", 0),
        })
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    states = sunlight.preview_day(cfg)
    return jsonify(states)


# ── HTML / CSS / JS (single-page app) ──────────────────────────────

_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NanoLeaf Sunlight Simulator — Quicksilver Industries LTD.</title>
<style>
  :root {
    --bg: #111827;
    --card: #1b2535;
    --accent: #27364b;
    --text: #f7f3e8;
    --text2: #b8c0cc;
    --glow: #e8954f;
    --green: #68d5ad;
    --red: #ff7185;
    --border: #35445a;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    padding: 20px;
  }
  h1 {
    text-align: center;
    font-size: 1.4em;
    font-weight: 600;
    margin-bottom: 2px;
    color: var(--text);
  }
  h1 span { color: var(--glow); }
  .brand-byline {
    text-align: center; color: var(--text2); font-size: .78em;
    letter-spacing: .08em; text-transform: uppercase; margin-bottom: 20px;
  }
  .legal-notice {
    max-width: 1080px; margin: 18px auto 0; color: var(--text2);
    font-size: .78em; line-height: 1.5; text-align: center;
  }
  .legal-notice a { color: var(--glow); }
  .grid {
    display: grid;
    grid-template-columns: 1fr;
    gap: 16px;
    max-width: 1080px;
    margin: 0 auto;
  }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 18px;
  }
  .automation-card { order: -1; }
  .sim-hero {
    display: grid; grid-template-columns: minmax(220px, 300px) 1fr;
    gap: 20px; align-items: center; margin-bottom: 16px;
  }
  .house-scene {
    position: relative; overflow: hidden; border: 1px solid var(--border);
    border-radius: 12px; background: #17263a; min-height: 176px;
  }
  .house-scene svg { display: block; width: 100%; height: auto; }
  .scene-sky { fill: #203b5b; transition: fill 1s ease; }
  .scene-ground { fill: #233c35; }
  .scene-sun { fill: #ffd080; transition: opacity 1s ease; }
  .scene-cloud { fill: #c8d1da; opacity: .88; }
  .scene-rain { stroke: #78b8e6; stroke-width: 3; stroke-linecap: round; }
  .scene-weather-cloud, .scene-weather-rain { opacity: 0; transition: opacity .5s ease; }
  .house-scene[data-weather="cloud"] .scene-weather-cloud,
  .house-scene[data-weather="rain"] .scene-weather-cloud,
  .house-scene[data-weather="rain"] .scene-weather-rain { opacity: 1; }
  .house-scene[data-weather="cloud"] .scene-sun { opacity: .35; }
  .house-scene[data-weather="rain"] .scene-sun { opacity: .12; }
  .scene-house { fill: #e5ddd0; stroke: #111827; stroke-width: 3; }
  .scene-roof { fill: #a85f4d; stroke: #111827; stroke-width: 3; }
  .scene-door { fill: #665044; }
  .scene-window { fill: #ffd080; stroke: #111827; stroke-width: 3; transition: fill 1s ease, opacity 1s ease; }
  .scene-compass { fill: rgba(17,24,39,.88); stroke: var(--border); stroke-width: 1; }
  .scene-needle { fill: var(--glow); transform-origin: 211px 124px; transition: transform .6s ease; }
  .scene-north { fill: var(--text); font: 700 8px sans-serif; text-anchor: middle; }
  .scene-copy { min-width: 0; }
  .scene-kicker { color: var(--glow); font-size: .78em; text-transform: uppercase; letter-spacing: .1em; }
  .scene-title { font-size: 1.55em; line-height: 1.15; margin: 5px 0 12px; }
  .scene-facts { display: grid; gap: 8px; }
  .scene-fact { display: grid; grid-template-columns: 22px 92px 1fr; gap: 7px; align-items: baseline; font-size: .88em; }
  .scene-fact .fact-icon { color: var(--glow); text-align: center; }
  .scene-fact .fact-label { color: var(--text2); }
  .scene-fact .fact-value { color: var(--text); font-weight: 600; min-width: 0; }
  .status-strip {
    display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px;
  }
  .status-pill {
    min-height: 32px; display: inline-flex; align-items: center; gap: 7px;
    padding: 5px 10px; border: 1px solid var(--border); border-radius: 999px;
    color: var(--text2); font-size: .82em; background: rgba(255,255,255,.025);
  }
  .status-pill strong { color: var(--text); }
  .offline { color: var(--red) !important; border-color: var(--red) !important; }
  .override { color: #ffd080 !important; border-color: #bd8640 !important; }
  .nap-panel {
    display: flex; align-items: center; justify-content: space-between;
    flex-wrap: wrap; gap: 14px; margin-bottom: 16px; padding: 14px;
    border: 1px solid #8f6337; border-radius: 12px;
    background: linear-gradient(135deg, rgba(255,106,0,.12), rgba(255,208,128,.035));
  }
  .nap-copy { display: grid; gap: 3px; min-width: 220px; flex: 1; }
  .nap-copy strong { color: #ffd080; }
  .nap-copy span { color: var(--text2); font-size: .82em; }
  .nap-controls { display: flex; align-items: end; flex-wrap: wrap; gap: 8px; }
  .nap-field { display: grid; gap: 3px; }
  .nap-field label { color: var(--text2); font-size: .72em; }
  .nap-field input {
    width: 78px; min-height: 44px; padding: 7px 8px;
    color: var(--text); background: var(--accent);
    border: 1px solid var(--border); border-radius: 8px;
  }
  .card h2 {
    font-size: 0.85em;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text2);
    margin-bottom: 14px;
  }
  /* Device info */
  .info-grid {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 4px 12px;
    font-size: 0.9em;
  }
  .info-grid .label { color: var(--text2); }
  .info-grid .value { font-weight: 500; }
  /* Power button */
  .power-row {
    display: flex;
    align-items: center;
    gap: 14px;
    margin-bottom: 14px;
  }
  .power-btn {
    width: 52px; height: 52px;
    border-radius: 50%;
    border: 2px solid var(--border);
    background: var(--accent);
    color: var(--text2);
    font-size: 1.3em;
    cursor: pointer;
    transition: all 0.2s;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .power-btn.on {
    border-color: var(--green);
    color: var(--green);
    box-shadow: 0 0 12px rgba(78, 204, 163, 0.3);
  }
  .power-label { font-size: 0.9em; color: var(--text2); }
  /* Sliders */
  .slider-row {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 10px;
  }
  .slider-row label {
    font-size: 0.85em;
    color: var(--text2);
    min-width: 70px;
  }
  .slider-row input[type=range] {
    flex: 1;
    accent-color: var(--glow);
    height: 6px;
  }
  .slider-row .val {
    font-size: 0.85em;
    min-width: 40px;
    text-align: right;
  }
  /* Color presets */
  .color-presets {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 10px;
  }
  .color-swatch {
    width: 44px; height: 44px; padding: 0;
    border-radius: 50%;
    border: 2px solid var(--border);
    cursor: pointer;
    transition: transform 0.15s, border-color 0.15s;
  }
  .color-swatch:hover {
    transform: scale(1.15);
    border-color: var(--text);
  }
  .color-input-row {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .color-input-row input[type=color] {
    width: 40px; height: 34px;
    border: none; padding: 0;
    background: none; cursor: pointer;
  }
  .color-input-row input[type=text] {
    flex: 1;
    background: var(--accent);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    padding: 6px 10px;
    font-size: 0.9em;
  }
  .color-input-row button { margin-left: 4px; }
  /* Effects */
  .effects-list {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .effect-chip {
    min-height: 44px; padding: 8px 14px;
    border-radius: 16px;
    font-size: 0.82em;
    background: var(--accent);
    border: 1px solid var(--border);
    color: var(--text2);
    cursor: pointer;
    transition: all 0.15s;
  }
  .effect-chip:hover { border-color: var(--text); color: var(--text); }
  .effect-chip.active {
    border-color: var(--green);
    color: var(--green);
    background: rgba(78, 204, 163, 0.1);
  }
  /* Sunlight */
  .sim-status {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 12px;
  }
  .dot {
    width: 10px; height: 10px;
    border-radius: 50%;
    background: var(--text2);
  }
  .dot.running { background: var(--green); box-shadow: 0 0 6px rgba(78, 204, 163, 0.5); }
  .sim-detail {
    font-size: 0.85em;
    color: var(--text2);
    line-height: 1.6;
    margin-bottom: 12px;
  }
  .sim-detail strong { color: var(--text); }
  .sim-light-preview {
    height: 8px;
    border-radius: 4px;
    margin-bottom: 14px;
    transition: background 1s;
  }
  .sim-form {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    margin-bottom: 12px;
  }
  .sim-form label {
    font-size: 0.8em;
    color: var(--text2);
    display: block;
    margin-bottom: 2px;
  }
  .sim-form input, .sim-form select {
    width: 100%;
    background: var(--accent);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    padding: 6px 8px;
    font-size: 0.85em;
  }
  /* Buttons */
  button {
    background: var(--accent);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    min-height: 44px;
    padding: 8px 16px;
    font-size: 0.85em;
    cursor: pointer;
    transition: all 0.15s;
  }
  button:hover { border-color: var(--text2); }
  button.primary {
    background: var(--glow);
    border-color: var(--glow);
    color: #fff;
  }
  button.primary:hover { opacity: 0.9; }
  button.green {
    background: var(--green);
    border-color: var(--green);
    color: #1a1a2e;
    font-weight: 600;
  }
  button:focus-visible, input:focus-visible, select:focus-visible {
    outline: 3px solid #ffd080; outline-offset: 3px;
  }
  .btn-row { display: flex; gap: 8px; }
  /* Timeline */
  .timeline {
    height: 150px;
    display: grid;
    grid-template-columns: repeat(48, minmax(0, 1fr));
    align-items: end;
    gap: 2px;
    overflow: hidden;
    font-size: 0.78em;
    font-family: monospace;
    line-height: 1.5;
    color: var(--text2);
    scrollbar-width: thin;
    scrollbar-color: var(--border) transparent;
  }
  .timeline .t-row { display: flex; gap: 8px; }
  .timeline .t-row { min-width: 0; align-items: end; height: 100%; }
  .timeline .t-row span:not(.t-bar) { display: none; }
  .timeline .t-time { min-width: 42px; color: var(--text2); }
  .timeline .t-phase { min-width: 90px; }
  .timeline .t-value { flex: 1; }
  .timeline .t-br { min-width: 36px; text-align: right; }
  .timeline .t-bar {
    width: 100%;
    min-height: 3px;
    border-radius: 3px;
    display: inline-block;
    vertical-align: middle;
    margin-right: 6px;
  }
  /* Toast */
  .toast {
    position: fixed;
    bottom: 20px;
    left: 50%;
    transform: translateX(-50%) translateY(80px);
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 20px;
    font-size: 0.85em;
    opacity: 0;
    transition: all 0.3s;
    z-index: 100;
  }
  .toast.show {
    transform: translateX(-50%) translateY(0);
    opacity: 1;
  }
  .toast.error { border-color: var(--glow); }
  .diagnostics { margin-top: 14px; border-top: 1px solid var(--border); padding-top: 12px; }
  @media (min-width: 800px) {
    .grid { grid-template-columns: 1fr 1fr; }
    .automation-card { grid-column: 1 / -1; }
  }
  @media (max-width: 520px) {
    body { padding: 12px; }
    .card { padding: 15px; }
    .sim-form { grid-template-columns: 1fr; }
    .btn-row { flex-wrap: wrap; }
    .sim-hero { grid-template-columns: 1fr; gap: 14px; }
    .house-scene { min-height: 0; }
    .scene-title { font-size: 1.3em; }
    .nap-panel, .nap-controls { align-items: stretch; }
    .nap-controls { width: 100%; }
    .nap-field { flex: 1; }
    .nap-field input { width: 100%; }
  }
</style>
</head>
<body>

<h1><span>NanoLeaf</span> Sunlight Simulator</h1>
<div class="brand-byline">by Quicksilver Industries LTD.</div>

<div class="grid">

  <!-- Device Info -->
  <div class="card" id="card-info">
    <h2>Device</h2>
    <div class="power-row">
      <button class="power-btn" id="powerBtn" onclick="togglePower()" title="Toggle power" aria-label="Toggle Nanoleaf power">&#x23FB;</button>
      <span class="power-label" id="powerLabel">--</span>
    </div>
    <div class="info-grid" id="infoGrid">
      <span class="label">Name</span><span class="value" id="devName">--</span>
      <span class="label">Model</span><span class="value" id="devModel">--</span>
      <span class="label">Panels</span><span class="value" id="devPanels">--</span>
      <span class="label">Firmware</span><span class="value" id="devFw">--</span>
      <span class="label">Effect</span><span class="value" id="devEffect">--</span>
    </div>
  </div>

  <!-- Brightness & Color Temp -->
  <div class="card">
    <h2>Brightness &amp; Color Temperature</h2>
    <div class="slider-row">
      <label for="brSlider">Brightness</label>
      <input type="range" id="brSlider" min="0" max="100" value="50"
             oninput="document.getElementById('brVal').textContent=this.value+'%'"
             onchange="setBrightness(this.value)">
      <span class="val" id="brVal">50%</span>
    </div>
    <div class="slider-row">
      <label for="ctSlider">Color Temp</label>
      <input type="range" id="ctSlider" min="1200" max="6500" step="1" value="4000"
             oninput="document.getElementById('ctVal').textContent=this.value+'K'"
             onchange="setColorTemp(this.value)">
      <span class="val" id="ctVal">4000K</span>
    </div>
  </div>

  <!-- Color -->
  <div class="card">
    <h2>Color</h2>
    <div class="color-presets" id="colorPresets"></div>
    <div class="color-input-row">
      <input type="color" id="colorPicker" value="#ffffff" aria-label="Custom color" onchange="setColor(this.value)">
      <input type="text" id="colorText" aria-label="Color name or value" placeholder="red, #ff0000, 255,0,0">
      <button onclick="setColor(document.getElementById('colorText').value)">Set</button>
    </div>
  </div>

  <!-- Effects -->
  <div class="card">
    <h2>Effects</h2>
    <div class="effects-list" id="effectsList">loading...</div>
  </div>

  <!-- Sunlight Simulator -->
  <div class="card automation-card">
    <h2>Window Light Simulator</h2>
    <div class="sim-hero">
      <div class="house-scene" id="houseScene" data-weather="clear">
        <svg viewBox="0 0 240 150" role="img" id="houseGraphic" aria-label="Configured home and current simulated sunlight">
          <rect class="scene-sky" width="240" height="112" rx="10" />
          <circle class="scene-sun" cx="42" cy="35" r="17" />
          <g class="scene-weather-cloud">
            <circle class="scene-cloud" cx="61" cy="39" r="13" />
            <circle class="scene-cloud" cx="77" cy="31" r="18" />
            <circle class="scene-cloud" cx="96" cy="40" r="14" />
            <rect class="scene-cloud" x="60" y="39" width="39" height="13" rx="6" />
          </g>
          <g class="scene-weather-rain">
            <line x1="67" y1="57" x2="63" y2="66" /><line x1="81" y1="57" x2="77" y2="66" /><line x1="95" y1="57" x2="91" y2="66" />
          </g>
          <rect class="scene-ground" y="111" width="240" height="39" />
          <path class="scene-house" d="M62 74h100v61H62z" />
          <path class="scene-roof" d="M51 78l61-48 61 48z" />
          <rect class="scene-door" x="75" y="101" width="23" height="34" rx="2" />
          <rect class="scene-window" id="sceneWindow" x="119" y="91" width="28" height="25" rx="2" />
          <path d="M133 91v25M119 103.5h28" stroke="#111827" stroke-width="2" />
          <circle class="scene-compass" cx="211" cy="124" r="20" />
          <text class="scene-north" x="211" y="109">N</text>
          <path class="scene-needle" id="sceneNeedle" d="M211 108l5 16-5 10-5-10z" />
        </svg>
      </div>
      <div class="scene-copy" aria-live="polite">
        <div class="scene-kicker">Your configured daylight</div>
        <div class="scene-title" id="sceneTitle">Reading the sky…</div>
        <div class="scene-facts">
          <div class="scene-fact"><span class="fact-icon" aria-hidden="true">⌖</span><span class="fact-label">Location</span><span class="fact-value" id="sceneLocation">—</span></div>
          <div class="scene-fact"><span class="fact-icon" aria-hidden="true">↗</span><span class="fact-label">Orientation</span><span class="fact-value" id="sceneOrientation">—</span></div>
          <div class="scene-fact"><span class="fact-icon" aria-hidden="true">☁</span><span class="fact-label">Weather</span><span class="fact-value" id="sceneWeather">—</span></div>
          <div class="scene-fact"><span class="fact-icon" aria-hidden="true">☀</span><span class="fact-label">Light</span><span class="fact-value" id="sceneLight">—</span></div>
        </div>
      </div>
    </div>
    <div class="status-strip" aria-live="polite">
      <span class="status-pill" id="deviceStatus"><strong>Device</strong> Checking</span>
      <span class="status-pill" id="controlStatus"><strong>Control</strong> Checking</span>
      <span class="status-pill" id="weatherStatus"><strong>Weather</strong> Checking</span>
    </div>
    <div class="nap-panel" aria-live="polite">
      <div class="nap-copy">
        <strong>Nap Mode</strong>
        <span id="napStatus">Dim warm amber light for a timed rest, then resume daylight.</span>
      </div>
      <div class="nap-controls">
        <div class="nap-field">
          <label for="napMinutes">Minutes</label>
          <input type="number" id="napMinutes" value="40" min="5" max="180">
        </div>
        <div class="nap-field">
          <label for="napBrightness">Brightness %</label>
          <input type="number" id="napBrightness" value="5" min="1" max="20">
        </div>
        <button class="primary" id="napStartBtn" onclick="startNap()">Start nap</button>
        <button id="napStopBtn" onclick="stopNap()" style="display:none">End nap</button>
      </div>
    </div>
    <div class="sim-status">
      <span class="dot" id="simDot"></span>
      <span id="simLabel">Stopped</span>
    </div>
    <div class="sim-light-preview" id="simPreview"></div>
    <div class="sim-detail" id="simDetail"></div>
    <div class="sim-form" id="simForm">
      <div>
        <label for="simLat">Latitude</label>
        <input type="number" id="simLat" value="34.13" step="0.01">
      </div>
      <div>
        <label for="simLon">Longitude</label>
        <input type="number" id="simLon" value="-84.34" step="0.01">
      </div>
      <div>
        <label for="simFacing">Facing</label>
        <select id="simFacing">
          <option value="north">North</option>
          <option value="northeast">Northeast</option>
          <option value="east">East</option>
          <option value="southeast">Southeast</option>
          <option value="south">South</option>
          <option value="southwest" selected>Southwest</option>
          <option value="west">West</option>
          <option value="northwest">Northwest</option>
        </select>
      </div>
      <div>
        <label for="simPeak">Peak Brightness</label>
        <input type="number" id="simPeak" value="75" min="1" max="100">
      </div>
      <div>
        <label for="simBias">Brightness Bias</label>
        <input type="number" id="simBias" value="0" min="-50" max="50">
      </div>
    </div>
    <div class="btn-row">
      <button class="green" id="simStartBtn" onclick="startSim()">Start</button>
      <button class="primary" id="simDemoBtn" onclick="startDemo()">Demo</button>
      <button id="simStopBtn" onclick="stopSim()" style="display:none">Stop</button>
      <button id="simResumeBtn" onclick="resumeSim()" style="display:none">Resume automation</button>
      <button onclick="loadPreview()">Preview Day</button>
    </div>
    <div class="timeline" id="timeline" style="display:none; margin-top: 14px;"></div>
    <div class="diagnostics">
      <button onclick="toggleLog()" aria-controls="simLog" aria-expanded="false" id="logBtn">Diagnostics</button>
    </div>
    <pre class="sim-log" id="simLog" style="display:none; max-height:300px; overflow-y:auto; background:#111; color:#0f0; padding:10px; font-size:12px; margin-top:8px; border-radius:6px; white-space:pre-wrap;"></pre>
  </div>

</div>

<div class="legal-notice">
  Independent experimental software; not affiliated with, sponsored, endorsed,
  authorized, or supported by Nanoleaf Canada Ltd. Follow
  <a href="https://support.nanoleaf.me/hc/en-us" target="_blank" rel="noopener noreferrer">official Nanoleaf instructions</a>
  for your model before making changes. Third-party control can cause unexpected
  operation or damage and may affect warranty coverage. Use at your own risk.
</div>

<div class="toast" id="toast" role="status" aria-live="polite"></div>

<script>
const COLORS = [
  {name:'red',    hex:'#ff0000'},
  {name:'orange', hex:'#ffa500'},
  {name:'yellow', hex:'#ffff00'},
  {name:'green',  hex:'#00ff00'},
  {name:'cyan',   hex:'#00ffff'},
  {name:'blue',   hex:'#0000ff'},
  {name:'purple', hex:'#800080'},
  {name:'pink',   hex:'#ff69b4'},
  {name:'warm',   hex:'#ffb464'},
  {name:'cool',   hex:'#64b4ff'},
  {name:'white',  hex:'#ffffff'},
];

// ── Init ──

(function init() {
  const presets = document.getElementById('colorPresets');
  COLORS.forEach(c => {
    const el = document.createElement('button');
    el.type = 'button';
    el.className = 'color-swatch';
    el.style.background = c.hex;
    el.title = c.name;
    el.setAttribute('aria-label', 'Set color to ' + c.name);
    el.onclick = () => setColor(c.name);
    presets.appendChild(el);
  });
  refresh();
  loadEffects();
  setInterval(pollSim, 5000);
  setInterval(refresh, 15000);
  pollSim();
})();

// ── Toast ──

function toast(msg, err) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show' + (err ? ' error' : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.className = 'toast', 2500);
}

// ── API helpers ──

async function api(path, opts) {
  try {
    const r = await fetch(path, opts);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    return d;
  } catch(e) {
    toast(e.message, true);
    throw e;
  }
}

function post(path, body) {
  return api(path, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
}

// ── Device info ──

async function refresh() {
  try {
    const d = await api('/api/info');
    document.getElementById('devName').textContent = d.name;
    document.getElementById('devModel').textContent = d.model;
    document.getElementById('devPanels').textContent = d.num_panels;
    document.getElementById('devFw').textContent = d.firmware;
    document.getElementById('devEffect').textContent = d.effect;
    const btn = document.getElementById('powerBtn');
    const lbl = document.getElementById('powerLabel');
    if (d.power) {
      btn.classList.add('on');
      lbl.textContent = 'On — ' + d.brightness + '%';
    } else {
      btn.classList.remove('on');
      lbl.textContent = 'Off';
    }
    document.getElementById('brSlider').value = d.brightness;
    document.getElementById('brVal').textContent = d.brightness + '%';
    if (d.color_temp) {
      document.getElementById('ctSlider').value = d.color_temp;
      document.getElementById('ctVal').textContent = d.color_temp + 'K';
    }
  } catch(e) {}
}

// ── Power ──

async function togglePower() {
  await post('/api/power', {action:'toggle'});
  setTimeout(refresh, 300);
}

// ── Brightness ──

let _brTimeout;
async function setBrightness(val) {
  clearTimeout(_brTimeout);
  _brTimeout = setTimeout(async () => {
    await post('/api/brightness', {level: parseInt(val)});
  }, 200);
}

// ── Color temp ──

let _ctTimeout;
async function setColorTemp(val) {
  clearTimeout(_ctTimeout);
  _ctTimeout = setTimeout(async () => {
    await post('/api/color-temp', {temp: parseInt(val)});
  }, 200);
}

// ── Color ──

async function setColor(val) {
  if (!val) return;
  // Convert #hex from color picker to hex string
  if (val.startsWith('#') && val.length === 7) {
    val = val.substring(1);
  }
  await post('/api/color', {color: val});
  toast('Color set');
}

// ── Effects ──

async function loadEffects() {
  try {
    const d = await api('/api/effects');
    const el = document.getElementById('effectsList');
    el.innerHTML = '';
    d.effects.forEach(name => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'effect-chip' + (name === d.current ? ' active' : '');
      chip.textContent = name;
      chip.onclick = () => setEffect(name);
      el.appendChild(chip);
    });
  } catch(e) {}
}

async function setEffect(name) {
  await post('/api/effect', {name});
  toast('Effect: ' + name);
  loadEffects();
  setTimeout(refresh, 500);
}

// ── Sunlight simulator ──

function simCfg() {
  return {
    lat: parseFloat(document.getElementById('simLat').value),
    lon: parseFloat(document.getElementById('simLon').value),
    facing: document.getElementById('simFacing').value,
    peak: parseInt(document.getElementById('simPeak').value),
    bias: parseInt(document.getElementById('simBias').value) || 0,
  };
}

async function startSim() {
  await post('/api/sunlight/start', simCfg());
  toast('Simulator started');
  pollSim();
}

async function startDemo() {
  const cfg = simCfg();
  cfg.demo = true;
  await post('/api/sunlight/start', cfg);
  toast('Demo started — 24h cycle in ~8 min');
  pollSim();
}

async function stopSim() {
  await post('/api/sunlight/stop', {});
  toast('Simulator stopped');
  pollSim();
}

async function resumeSim() {
  await post('/api/sunlight/resume', {});
  toast('Automation resuming');
  pollSim();
}

async function startNap() {
  const minutes = parseInt(document.getElementById('napMinutes').value);
  const brightness = parseInt(document.getElementById('napBrightness').value);
  const d = await post('/api/nap/start', {minutes, brightness});
  const until = new Date(d.until * 1000).toLocaleTimeString([], {hour:'numeric', minute:'2-digit'});
  toast('Nap Mode active until ' + until);
  pollSim();
  setTimeout(refresh, 500);
}

async function stopNap() {
  await post('/api/nap/stop', {});
  toast('Nap ended; daylight automation resuming');
  pollSim();
}

function stateColor(s) {
  if (!s) return '#333';
  if (s.mode === 'color' && s.rgb) {
    return 'rgb(' + s.rgb[0] + ',' + s.rgb[1] + ',' + s.rgb[2] + ')';
  }
  if (s.mode === 'color_temp') {
    // Approximate color temp to RGB for preview
    const t = (s.color_temp - 1200) / (6500 - 1200);
    const r = Math.round(255 - t * 55);
    const g = Math.round(180 + t * 50);
    const b = Math.round(100 + t * 155);
    return 'rgb(' + r + ',' + g + ',' + b + ')';
  }
  return '#222';
}

const FACING_ANGLES = {
  north: 0, northeast: 45, east: 90, southeast: 135,
  south: 180, southwest: 225, west: 270, northwest: 315,
};

function formatCoordinate(value, positive, negative) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '—';
  return Math.abs(number).toFixed(2) + '° ' + (number >= 0 ? positive : negative);
}

function updateHouseScene(d) {
  const cfg = d.config || simCfg();
  const s = d.state;
  const facing = (cfg.facing || 'southwest').toLowerCase();
  const weather = s && s.weather ? s.weather.replaceAll('_', ' ') : 'unavailable';
  const rawWeather = s && s.weather ? s.weather : '';
  const rainy = /rain|drizzle|thunder/.test(rawWeather);
  const cloudy = rainy || /cloud|overcast|fog|snow/.test(rawWeather);
  const scene = document.getElementById('houseScene');
  const graphic = document.getElementById('houseGraphic');
  const windowEl = document.getElementById('sceneWindow');
  const sky = scene.querySelector('.scene-sky');

  scene.dataset.weather = rainy ? 'rain' : (cloudy ? 'cloud' : 'clear');
  document.getElementById('sceneNeedle').style.transform =
    'rotate(' + (FACING_ANGLES[facing] || 0) + 'deg)';
  document.getElementById('sceneLocation').textContent =
    formatCoordinate(cfg.latitude, 'N', 'S') + ', ' + formatCoordinate(cfg.longitude, 'E', 'W');
  document.getElementById('sceneOrientation').textContent =
    facing.charAt(0).toUpperCase() + facing.slice(1) + '-facing window';
  document.getElementById('sceneWeather').textContent = s && s.weather
    ? weather + ' · ' + s.cloud_cover + '% cloud cover'
    : 'Weather unavailable';

  if (s) {
    const lightValue = s.mode === 'color_temp' ? s.color_temp + 'K' :
      (s.mode === 'color' ? 'Color light' : 'Lights off');
    document.getElementById('sceneTitle').textContent =
      s.phase.charAt(0).toUpperCase() + s.phase.slice(1) + ' at ' + s.brightness + '%';
    document.getElementById('sceneLight').textContent = lightValue + ' · ' + s.brightness + '% brightness';
    windowEl.style.fill = stateColor(s);
    windowEl.style.opacity = Math.max(.18, s.brightness / 100);
    sky.style.fill = /night/.test(s.phase) ? '#101a35' :
      (/dawn|sunrise|golden/.test(s.phase) ? '#76516a' : '#203b5b');
  } else {
    document.getElementById('sceneTitle').textContent = d.running ? 'Calculating daylight…' : 'Automation is stopped';
    document.getElementById('sceneLight').textContent = 'No current simulation';
    windowEl.style.fill = '#526174';
    windowEl.style.opacity = .35;
  }

  if (d.control_mode === 'nap' && d.nap) {
    const until = new Date(d.nap.until * 1000).toLocaleTimeString([], {hour:'numeric', minute:'2-digit'});
    document.getElementById('sceneTitle').textContent = 'Nap Mode at ' + d.nap.brightness + '%';
    document.getElementById('sceneLight').textContent = 'Warm amber · resumes at ' + until;
    windowEl.style.fill = 'rgb(' + d.nap.rgb.join(',') + ')';
    windowEl.style.opacity = Math.max(.18, d.nap.brightness / 100);
  }

  graphic.setAttribute('aria-label',
    'House at ' + document.getElementById('sceneLocation').textContent + ', ' +
    facing + '-facing window, weather ' + weather +
    (s ? ', simulated ' + s.phase + ' at ' + s.brightness + ' percent brightness' : ''));
}

async function pollSim() {
  try {
    const d = await api('/api/sunlight/status');
    const dot = document.getElementById('simDot');
    const label = document.getElementById('simLabel');
    const detail = document.getElementById('simDetail');
    const preview = document.getElementById('simPreview');
    const startBtn = document.getElementById('simStartBtn');
    const demoBtn = document.getElementById('simDemoBtn');
    const stopBtn = document.getElementById('simStopBtn');
    const resumeBtn = document.getElementById('simResumeBtn');
    const form = document.getElementById('simForm');
    const deviceStatus = document.getElementById('deviceStatus');
    const controlStatus = document.getElementById('controlStatus');
    const weatherStatus = document.getElementById('weatherStatus');
    const napStatus = document.getElementById('napStatus');
    const napStartBtn = document.getElementById('napStartBtn');
    const napStopBtn = document.getElementById('napStopBtn');

    updateHouseScene(d);

    deviceStatus.className = 'status-pill' + (d.device_online ? '' : ' offline');
    deviceStatus.innerHTML = '<strong>Device</strong> ' + (d.device_online ? 'Online' : 'Offline');
    const manualOverride = d.control_mode === 'manual_override';
    const napping = d.control_mode === 'nap' && d.nap;
    const overriding = manualOverride || napping;
    controlStatus.className = 'status-pill' + (overriding ? ' override' : '');
    if (napping) {
      const until = new Date(d.nap.until * 1000).toLocaleTimeString([], {hour:'numeric', minute:'2-digit'});
      controlStatus.innerHTML = '<strong>Control</strong> Nap until ' + until;
      napStatus.textContent = d.nap.brightness + '% warm amber · daylight resumes at ' + until;
    } else if (manualOverride && d.manual_override_until) {
      const until = new Date(d.manual_override_until * 1000).toLocaleTimeString([], {hour:'numeric', minute:'2-digit'});
      controlStatus.innerHTML = '<strong>Control</strong> Manual until ' + until;
      napStatus.textContent = 'Dim warm amber light for a timed rest, then resume daylight.';
    } else {
      controlStatus.innerHTML = '<strong>Control</strong> ' + (d.running ? 'Automation' : 'Stopped');
      napStatus.textContent = d.running
        ? 'Dim warm amber light for a timed rest, then resume daylight.'
        : 'Start sunlight automation before using Nap Mode.';
    }
    resumeBtn.style.display = manualOverride ? '' : 'none';
    napStartBtn.style.display = napping ? 'none' : '';
    napStartBtn.disabled = !d.running;
    napStopBtn.style.display = napping ? '' : 'none';

    const age = d.state && d.state.weather_age_seconds;
    if (d.state && d.state.weather) {
      const stale = age === null || age > 1800;
      weatherStatus.className = 'status-pill' + (stale ? ' override' : '');
      weatherStatus.innerHTML = '<strong>Weather</strong> ' + d.state.weather.replaceAll('_', ' ') +
        (stale ? ' (stale)' : '');
    } else {
      weatherStatus.className = 'status-pill';
      weatherStatus.innerHTML = '<strong>Weather</strong> Unavailable';
    }

    if (d.running) {
      dot.classList.toggle('running', d.device_online && !overriding);
      label.textContent = !d.device_online ? 'Device offline' :
        (napping ? 'Nap mode' : (manualOverride ? 'Manual override' : (d.demo ? 'Demo' : 'Running')));
      startBtn.style.display = 'none';
      demoBtn.style.display = 'none';
      stopBtn.style.display = '';
      form.style.display = 'none';

      if (napping) {
        const until = new Date(d.nap.until * 1000).toLocaleTimeString([], {hour:'numeric', minute:'2-digit'});
        detail.innerHTML = '<strong>nap mode</strong> &middot; warm amber &middot; ' +
          d.nap.brightness + '% &middot; until ' + until;
        preview.style.background = 'rgb(' + d.nap.rgb.join(',') + ')';
        preview.style.opacity = Math.max(.15, d.nap.brightness / 100);
      } else if (d.state) {
        const s = d.state;
        let val = '';
        if (s.mode === 'color' && s.rgb) val = 'RGB(' + s.rgb.join(', ') + ')';
        else if (s.mode === 'color_temp') val = s.color_temp + 'K';
        else val = 'off';
        let weather = '';
        if (s.weather) weather = ' &middot; ' + s.weather + ' ' + s.cloud_cover + '%';
        detail.innerHTML = '<strong>' + s.phase + '</strong> &middot; ' + val +
          ' &middot; ' + s.brightness + '%' + weather;
        preview.style.background = stateColor(s);
        const alpha = s.brightness / 100;
        preview.style.opacity = Math.max(0.15, alpha);
      }
      if (d.config) {
        const prefix = d.demo ? 'Demo' : 'Running';
        if (d.device_online && !overriding) label.textContent = prefix + ' (' + d.config.facing + '-facing)';
      }
    } else {
      dot.classList.remove('running');
      label.textContent = 'Stopped';
      startBtn.style.display = '';
      demoBtn.style.display = '';
      stopBtn.style.display = 'none';
      resumeBtn.style.display = 'none';
      napStartBtn.disabled = true;
      napStopBtn.style.display = 'none';
      form.style.display = '';
      detail.innerHTML = '';
      preview.style.background = '#333';
      preview.style.opacity = 1;
    }
  } catch(e) {}
}

// ── Day preview ──

async function loadPreview() {
  const cfg = simCfg();
  const q = new URLSearchParams({lat:cfg.lat, lon:cfg.lon, facing:cfg.facing, peak:cfg.peak});
  try {
    const states = await api('/api/sunlight/preview?' + q);
    const el = document.getElementById('timeline');
    el.style.display = '';
    el.innerHTML = '';
    states.forEach(s => {
      const row = document.createElement('div');
      row.className = 't-row';
      let val = '';
      if (s.mode === 'color' && s.rgb) val = 'RGB(' + s.rgb.join(',') + ')';
      else if (s.mode === 'color_temp') val = s.color_temp + 'K';
      else val = 'off';
      const bg = stateColor(s);
      row.innerHTML =
        '<span class="t-time">' + s.time + '</span>' +
        '<span class="t-bar" style="background:' + bg + ';opacity:' + Math.max(0.15, s.brightness/100) + '"></span>' +
        '<span class="t-phase">' + s.phase + '</span>' +
        '<span class="t-value">' + val + '</span>' +
        '<span class="t-br">' + s.brightness + '%</span>';
      row.title = s.time + ' — ' + s.phase + ', ' + s.brightness + '%';
      row.setAttribute('aria-label', row.title);
      row.querySelector('.t-bar').style.height = Math.max(3, s.brightness) + '%';
      el.appendChild(row);
    });
  } catch(e) {}
}

// ── Simulator log ──

let _logVisible = false;
let _logTimer = null;

function toggleLog() {
  const el = document.getElementById('simLog');
  _logVisible = !_logVisible;
  document.getElementById('logBtn').setAttribute('aria-expanded', String(_logVisible));
  el.style.display = _logVisible ? '' : 'none';
  if (_logVisible) {
    refreshLog();
    _logTimer = setInterval(refreshLog, 3000);
  } else if (_logTimer) {
    clearInterval(_logTimer);
    _logTimer = null;
  }
}

async function refreshLog() {
  try {
    const d = await api('/api/sunlight/log');
    const el = document.getElementById('simLog');
    el.textContent = d.lines.join('\\n');
    el.scrollTop = el.scrollHeight;
  } catch(e) {}
}
</script>
</body>
</html>
"""


def _auto_start_simulator() -> None:
    """Start the sunlight simulator automatically on launch."""
    global _sim_thread, _sim_config, _sim_running, _sim_generation, _sim_file_lock
    global _control_mode, _manual_override_until, _nap_brightness

    with _sim_lock:
        if _sim_running:
            return

    cfg = sunlight.WindowConfig(brightness_bias=-5)
    weather_cache = WeatherCache(cfg.latitude, cfg.longitude)
    try:
        nl = _get_nl()
    except Exception as exc:
        _setup_file_logging()
        _file_logger.error("Auto-start: unable to connect to device: %s", exc)
        return

    with _sim_lock:
        if _sim_running:
            return

        _sim_file_lock = config.acquire_sunlight_lock()
        if _sim_file_lock is None:
            _setup_file_logging()
            _file_logger.info("Auto-start: lock held by another instance, skipping")
            return

        _sim_config = cfg
        _sim_running = True
        _control_mode = "automation"
        _manual_override_until = None
        _nap_brightness = None
        _sim_generation += 1
        gen = _sim_generation

    _sim_thread = threading.Thread(
        target=_run_sim_loop, args=(nl, cfg, weather_cache, gen), daemon=True,
    )
    _sim_thread.start()
    _setup_file_logging()
    _file_logger.info("Auto-started sunlight simulator (bias=-5)")


def _watchdog_loop(interval: float) -> None:
    while not _watchdog_stop.wait(interval):
        _sd_notify("WATCHDOG=1")


def _start_watchdog() -> None:
    """Feed systemd's watchdog independently of simulator state."""
    global _watchdog_thread
    watchdog_usec = os.environ.get("WATCHDOG_USEC")
    watchdog_pid = os.environ.get("WATCHDOG_PID")
    try:
        if not watchdog_usec or (watchdog_pid and int(watchdog_pid) != os.getpid()):
            return
        interval = max(1.0, int(watchdog_usec) / 2_000_000)
    except ValueError:
        return
    _watchdog_stop.clear()
    _watchdog_thread = threading.Thread(
        target=_watchdog_loop,
        args=(interval,),
        daemon=True,
        name="systemd-watchdog",
    )
    _watchdog_thread.start()


def _start_auto_start_worker() -> None:
    """Connect to the device without delaying web-service readiness."""
    threading.Thread(
        target=_auto_start_simulator,
        daemon=True,
        name="sunlight-auto-start",
    ).start()


def run(host: str = "0.0.0.0", port: int = 5000, ip: str | None = None):
    """Start the web interface."""
    global _nl
    if ip:
        _nl = client.connect(ip)
    server = make_server(host, port, app, threaded=True)
    _start_watchdog()
    _sd_notify("READY=1")
    _start_auto_start_worker()
    try:
        server.serve_forever()
    finally:
        _watchdog_stop.set()
        _sd_notify("STOPPING=1")
        server.server_close()
