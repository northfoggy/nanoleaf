"""Web interface for nanoleaf-ctl.

Provides a browser-based dashboard for controlling Nanoleaf panels
and monitoring the sunlight simulator. Runs on the local network
so you can control everything from your phone.
"""

import json
import logging
import os
import socket
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, Response

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
    with _sock.socket(_sock.AF_UNIX, _sock.SOCK_DGRAM) as s:
        s.sendto(state.encode(), addr)


# ── Shared state for the sunlight simulator thread ──────────────────

_sim_lock = threading.Lock()
_sim_thread: threading.Thread | None = None
_sim_state: dict | None = None        # latest computed light state
_sim_config: sunlight.WindowConfig | None = None
_sim_running = False
_sim_demo = False                     # True when running in time-lapse demo mode
_sim_generation = 0                   # incremented on each start; old threads check & exit
_sim_file_lock = None                 # fcntl lock fd to prevent duplicates
_device_online = True
_sim_log: deque = deque(maxlen=200)


_file_logger = logging.getLogger("nanoleaf.sunlight")
_file_logger.setLevel(logging.DEBUG)
_log_handler = None


def _setup_file_logging() -> None:
    """Set up persistent file logging so crashes are traceable."""
    global _log_handler
    if _log_handler is not None:
        return
    log_dir = os.path.expanduser("~/.nanoleaf-ctl")
    os.makedirs(log_dir, exist_ok=True)
    _log_handler = logging.FileHandler(os.path.join(log_dir, "sunlight.log"))
    _log_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    _file_logger.addHandler(_log_handler)


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    _sim_log.append(f"[{ts}] {msg}")
    _file_logger.info(msg)


def _run_sim_loop(nl, cfg, weather_cache, my_generation, demo=False):
    """Background loop identical to sunlight.run_simulator but updating shared state.

    In demo mode, cycles through a full 24h day in ~8 minutes:
    each tick advances simulated time by 15 minutes, with 5s real intervals.
    """
    global _sim_state, _sim_running, _sim_file_lock, _device_online

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

    # Only release the lock if we're still the current generation
    # (if superseded, the new thread owns the lock now)
    if _sim_generation == my_generation:
        config.release_sunlight_lock(_sim_file_lock)
        _sim_file_lock = None
        _log("Simulator stopped")
    else:
        _log(f"Simulator gen={my_generation} exiting (superseded by gen={_sim_generation})")


def _run_sim_loop_inner(nl, cfg, weather_cache, my_generation, demo=False):
    global _sim_state, _sim_running, _sim_file_lock, _device_online

    hostname = socket.gethostname()
    last_state_key = None
    last_applied_brightness = None
    mode_label = "DEMO" if demo else "Simulator"
    _log(f"{mode_label} started (gen={my_generation}, host={hostname}, pid={__import__('os').getpid()}) — facing={cfg.facing}, lat={cfg.latitude}, lon={cfg.longitude}, peak={cfg.peak_brightness}")
    _log(f"Device URL: {nl.url}")

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

        state_key = (state["mode"], state.get("rgb"), state.get("color_temp"), state["brightness"])

        # Conflict detection: read back device brightness and compare to what we last set
        if not demo and last_applied_brightness is not None and _device_online:
            try:
                import requests
                actual_br = requests.get(nl.url + "/state/brightness", timeout=sunlight._API_TIMEOUT).json().get("value", 0)
                if abs(actual_br - last_applied_brightness) > 3:
                    _log(f"CONFLICT: device brightness is {actual_br}% but we last set {last_applied_brightness}% — another controller is likely active!")
            except Exception:
                pass  # device may be offline, handled below

        with _sim_lock:
            _sim_state = state

        applied = False
        try:
            if not _device_online or state_key != last_state_key:
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
            else:
                _log("No change, skipping apply")
        except Exception as e:
            _log(f"ERROR applying light: {e}")
            with _sim_lock:
                _device_online = False
                last_state_key = None
                last_applied_brightness = None

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

        # Ping systemd watchdog — we're alive and looping
        _sd_notify("WATCHDOG=1")

        _log(f"Sleeping {sleep_secs}s")
        # Sleep in short intervals so we can exit promptly on stop/supersede
        for _ in range(sleep_secs):
            if not _sim_running or _sim_generation != my_generation:
                break
            time.sleep(1)


# ── Flask app ───────────────────────────────────────────────────────

app = Flask(__name__)
# Store a reference to the Nanoleaf connection used by routes
_nl = None


def _get_nl():
    global _nl
    if _nl is None:
        _nl = client.connect()
    return _nl


# ── API routes ──────────────────────────────────────────────────────

@app.route("/")
def index():
    return Response(_HTML, content_type="text/html")


@app.route("/api/info")
def api_info():
    try:
        nl = _get_nl()
        info = client.get_info(nl)
        return jsonify(info)
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/power", methods=["POST"])
def api_power():
    try:
        nl = _get_nl()
        action = request.json.get("action", "toggle")
        if action == "on":
            nl.power_on()
        elif action == "off":
            nl.power_off()
        else:
            nl.toggle_power()
        state = "on" if nl.get_power() else "off"
        return jsonify({"power": state})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/brightness", methods=["POST"])
def api_brightness():
    try:
        nl = _get_nl()
        level = request.json.get("level", 50)
        level = max(0, min(100, int(level)))
        nl.set_brightness(level)
        return jsonify({"brightness": level})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/color", methods=["POST"])
def api_color():
    try:
        nl = _get_nl()
        color_str = request.json.get("color", "white")
        client.set_color_from_string(nl, color_str)
        return jsonify({"color": color_str})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/color-temp", methods=["POST"])
def api_color_temp():
    try:
        nl = _get_nl()
        temp = request.json.get("temp", 4000)
        temp = max(1200, min(6500, int(temp)))
        nl.set_color_temp(temp)
        return jsonify({"color_temp": temp})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/effects")
def api_effects():
    try:
        nl = _get_nl()
        effects = nl.list_effects()
        current = nl.get_current_effect()
        return jsonify({"effects": sorted(effects), "current": current})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/effect", methods=["POST"])
def api_effect():
    try:
        nl = _get_nl()
        name = request.json.get("name", "")
        available = nl.list_effects()
        if name not in available:
            return jsonify({"error": f"Effect '{name}' not found"}), 404
        nl.set_effect(name)
        return jsonify({"effect": name})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/sunlight/status")
def api_sunlight_status():
    with _sim_lock:
        running = _sim_running
        demo = _sim_demo
        state = dict(_sim_state) if _sim_state else None
        online = _device_online
        cfg = _sim_config
    result = {"running": running, "demo": demo, "device_online": online}
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

    # Thread-safe check-and-start: hold _sim_lock for the entire operation
    # to prevent two concurrent requests from both starting a loop
    with _sim_lock:
        if _sim_running:
            _log("Start requested but already running")
            return jsonify({"status": "already running"})

        # Acquire exclusive lock — prevents fighting with CLI instance
        _sim_file_lock = config.acquire_sunlight_lock()
        if _sim_file_lock is None:
            holder = config.read_lock_info() or "unknown"
            _log(f"Cannot start: lock held by {holder}")
            return jsonify({"status": "error", "error": f"Another sunlight instance is already running (held by {holder}). Stop it first."}), 409

        _sim_log.clear()
        data = request.json or {}
        demo = bool(data.get("demo", False))
        _log(f"Start request: {data}")
        cfg = sunlight.WindowConfig(
            latitude=data.get("lat", 34.13),
            longitude=data.get("lon", -84.34),
            timezone=data.get("tz", "America/New_York"),
            facing=data.get("facing", "southwest"),
            peak_brightness=data.get("peak", 75),
            night_off=not data.get("night_glow", False),
            brightness_bias=max(-50, min(50, int(data.get("bias", 0)))),
        )

        # Skip weather in demo mode — we want to see pure sun phases
        weather_cache = None
        if not demo and not data.get("no_weather", False):
            weather_cache = WeatherCache(cfg.latitude, cfg.longitude)

        nl = _get_nl()

        _sim_config = cfg
        _sim_running = True
        _sim_demo = demo
        _sim_generation += 1
        gen = _sim_generation

    _sim_thread = threading.Thread(
        target=_run_sim_loop, args=(nl, cfg, weather_cache, gen, demo), daemon=True,
    )
    _sim_thread.start()
    return jsonify({"status": "demo started" if demo else "started"})


@app.route("/api/sunlight/stop", methods=["POST"])
def api_sunlight_stop():
    global _sim_running, _sim_demo
    with _sim_lock:
        _sim_running = False
        _sim_demo = False
    # Lock is released in _run_sim_loop when it exits
    _log("Stop requested")
    return jsonify({"status": "stopped"})


@app.route("/api/sunlight/log")
def api_sunlight_log():
    return jsonify({"lines": list(_sim_log)})


@app.route("/api/sunlight/preview")
def api_sunlight_preview():
    lat = request.args.get("lat", 34.13, type=float)
    lon = request.args.get("lon", -84.34, type=float)
    tz = request.args.get("tz", "America/New_York")
    facing = request.args.get("facing", "southwest")
    peak = request.args.get("peak", 75, type=int)

    cfg = sunlight.WindowConfig(
        latitude=lat, longitude=lon, timezone=tz,
        facing=facing, peak_brightness=peak,
    )
    states = sunlight.preview_day(cfg)
    return jsonify(states)


# ── HTML / CSS / JS (single-page app) ──────────────────────────────

_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nanoleaf Control</title>
<style>
  :root {
    --bg: #1a1a2e;
    --card: #16213e;
    --accent: #0f3460;
    --text: #e0e0e0;
    --text2: #a0a0b0;
    --glow: #e94560;
    --green: #4ecca3;
    --border: #2a2a4a;
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
    margin-bottom: 20px;
    color: var(--text);
  }
  h1 span { color: var(--glow); }
  .grid {
    display: grid;
    grid-template-columns: 1fr;
    gap: 16px;
    max-width: 600px;
    margin: 0 auto;
  }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 18px;
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
    width: 32px; height: 32px;
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
    padding: 5px 12px;
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
  .btn-row { display: flex; gap: 8px; }
  /* Timeline */
  .timeline {
    max-height: 260px;
    overflow-y: auto;
    font-size: 0.78em;
    font-family: monospace;
    line-height: 1.5;
    color: var(--text2);
    scrollbar-width: thin;
    scrollbar-color: var(--border) transparent;
  }
  .timeline .t-row { display: flex; gap: 8px; }
  .timeline .t-time { min-width: 42px; color: var(--text2); }
  .timeline .t-phase { min-width: 90px; }
  .timeline .t-value { flex: 1; }
  .timeline .t-br { min-width: 36px; text-align: right; }
  .timeline .t-bar {
    width: 30px;
    height: 12px;
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
</style>
</head>
<body>

<h1><span>nanoleaf</span>-ctl</h1>

<div class="grid">

  <!-- Device Info -->
  <div class="card" id="card-info">
    <h2>Device</h2>
    <div class="power-row">
      <button class="power-btn" id="powerBtn" onclick="togglePower()" title="Toggle power">&#x23FB;</button>
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
      <label>Brightness</label>
      <input type="range" id="brSlider" min="0" max="100" value="50"
             oninput="document.getElementById('brVal').textContent=this.value+'%'"
             onchange="setBrightness(this.value)">
      <span class="val" id="brVal">50%</span>
    </div>
    <div class="slider-row">
      <label>Color Temp</label>
      <input type="range" id="ctSlider" min="1200" max="6500" step="100" value="4000"
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
      <input type="color" id="colorPicker" value="#ffffff" onchange="setColor(this.value)">
      <input type="text" id="colorText" placeholder="red, #ff0000, 255,0,0">
      <button onclick="setColor(document.getElementById('colorText').value)">Set</button>
    </div>
  </div>

  <!-- Effects -->
  <div class="card">
    <h2>Effects</h2>
    <div class="effects-list" id="effectsList">loading...</div>
  </div>

  <!-- Sunlight Simulator -->
  <div class="card">
    <h2>Window Light Simulator</h2>
    <div class="sim-status">
      <span class="dot" id="simDot"></span>
      <span id="simLabel">Stopped</span>
    </div>
    <div class="sim-light-preview" id="simPreview"></div>
    <div class="sim-detail" id="simDetail"></div>
    <div class="sim-form" id="simForm">
      <div>
        <label>Latitude</label>
        <input type="number" id="simLat" value="34.13" step="0.01">
      </div>
      <div>
        <label>Longitude</label>
        <input type="number" id="simLon" value="-84.34" step="0.01">
      </div>
      <div>
        <label>Facing</label>
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
        <label>Peak Brightness</label>
        <input type="number" id="simPeak" value="75" min="1" max="100">
      </div>
      <div>
        <label>Brightness Bias</label>
        <input type="number" id="simBias" value="0" min="-50" max="50">
      </div>
    </div>
    <div class="btn-row">
      <button class="green" id="simStartBtn" onclick="startSim()">Start</button>
      <button class="primary" id="simDemoBtn" onclick="startDemo()">Demo</button>
      <button id="simStopBtn" onclick="stopSim()" style="display:none">Stop</button>
      <button onclick="loadPreview()">Preview Day</button>
    </div>
    <div class="timeline" id="timeline" style="display:none; margin-top: 14px;"></div>
    <div style="margin-top: 14px;">
      <button onclick="toggleLog()">Toggle Log</button>
    </div>
    <pre class="sim-log" id="simLog" style="display:none; max-height:300px; overflow-y:auto; background:#111; color:#0f0; padding:10px; font-size:12px; margin-top:8px; border-radius:6px; white-space:pre-wrap;"></pre>
  </div>

</div>

<div class="toast" id="toast"></div>

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
    const el = document.createElement('div');
    el.className = 'color-swatch';
    el.style.background = c.hex;
    el.title = c.name;
    el.onclick = () => setColor(c.name);
    presets.appendChild(el);
  });
  refresh();
  loadEffects();
  setInterval(pollSim, 5000);
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
      const chip = document.createElement('span');
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
    const form = document.getElementById('simForm');

    if (d.running) {
      dot.classList.add('running');
      label.textContent = d.demo ? 'Demo' : 'Running';
      startBtn.style.display = 'none';
      demoBtn.style.display = 'none';
      stopBtn.style.display = '';
      form.style.display = 'none';

      if (d.state) {
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
        label.textContent = prefix + ' (' + d.config.facing + '-facing)';
      }
    } else {
      dot.classList.remove('running');
      label.textContent = 'Stopped';
      startBtn.style.display = '';
      demoBtn.style.display = '';
      stopBtn.style.display = 'none';
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

    with _sim_lock:
        if _sim_running:
            return

        _sim_file_lock = config.acquire_sunlight_lock()
        if _sim_file_lock is None:
            _setup_file_logging()
            _file_logger.info("Auto-start: lock held by another instance, skipping")
            return

        cfg = sunlight.WindowConfig(brightness_bias=-5)
        weather_cache = WeatherCache(cfg.latitude, cfg.longitude)
        nl = _get_nl()

        _sim_config = cfg
        _sim_running = True
        _sim_generation += 1
        gen = _sim_generation

    _sim_thread = threading.Thread(
        target=_run_sim_loop, args=(nl, cfg, weather_cache, gen), daemon=True,
    )
    _sim_thread.start()
    _setup_file_logging()
    _file_logger.info("Auto-started sunlight simulator (bias=-5)")


def run(host: str = "0.0.0.0", port: int = 5000, ip: str | None = None):
    """Start the web interface."""
    global _nl
    if ip:
        _nl = client.connect(ip)
    _sd_notify("READY=1")
    _auto_start_simulator()
    app.run(host=host, port=port, debug=False)
