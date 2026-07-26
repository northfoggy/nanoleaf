# nanoleaf-ctl Architecture

## Overview

**nanoleaf-ctl** is a command-line and web-based controller for Nanoleaf light panels featuring a weather-aware sunlight simulator that mimics natural window light throughout the day. It runs on a Raspberry Pi (or any Linux server) on the same network as the Nanoleaf device.

## How It Works

The core idea: instead of simulating the sun directly, the simulator computes what light looks like coming *through a window* — with real colors (blue hour is blue, golden hour is orange), diffused brightness, and window-orientation awareness. It uses real solar position data for your latitude/longitude, modulated by live weather conditions.

```
 Solar Position (astral)    Weather (Open-Meteo)
        |                          |
        v                          v
 compute_window_light()  -->  apply_weather()
        |
        v
   apply_light()  -->  Nanoleaf API  -->  Physical Panels
```

Every 60 seconds, the loop:
1. Computes solar elevation and azimuth for the current time and location
2. Maps elevation to a phase (night, twilight, blue hour, golden hour, day, midday)
3. Calculates brightness and color based on phase + window facing direction
4. Fetches weather (cached, refreshes every 10 min) and modulates accordingly
5. Sends the result to the Nanoleaf device

## Project Structure

```
nanoleaf_ctl/
  __init__.py         # Package init
  cli.py              # CLI entry point, 17 subcommands
  client.py           # Nanoleaf API wrapper (discovery, pairing, control)
  config.py           # Persistent config (~/.config/nanoleaf-ctl/) + file locking
  sunlight.py         # Core algorithm: solar phases, weather modulation, main loop
  weather.py          # Open-Meteo integration with 10-min cache
  web.py              # Flask web dashboard (single-page app, embedded HTML/JS)
```

## Module Details

### sunlight.py — The Engine

The main algorithm in `compute_window_light()` maps solar elevation to light phases:

| Elevation | Phase | Mode | Description |
|-----------|-------|------|-------------|
| < -18 | night | off / dim glow | Full dark |
| -18 to -6 | twilight | RGB (5,5,15)→(30,50,120) | Deep blue, very dim |
| -6 to 0 | blue hour | RGB blue-purple | Rich blue through window |
| 0 to 6 | golden hour | RGB (255,130,50)↔(255,200,130) | Warm orange/amber |
| 6 to 15 | morning/afternoon | color_temp 2700-4000K | Transitional warm white |
| 15 to 40 | day | color_temp 4000-5500K | Neutral daylight |
| > 40 | midday | color_temp 5500K | Bright cool white |

**Window facing** adjusts brightness based on sun-to-window angle:
- Direct (0-45 difference): full brightness
- Angled (45-90): moderate
- Oblique (90-135): weak indirect
- Behind (>135): ambient only (10%)

**Weather modulation** (`apply_weather()`):
- Cloud cover dims brightness: clear (0%) → overcast (-40%) → rain (-55%) → storm (-65%)
- Overcast desaturates RGB colors toward grey
- Cloudy days shift color temperature cooler (up to +500K)

### weather.py — Live Weather

Uses the free Open-Meteo API (no API key needed). The `WeatherCache` class fetches cloud cover and weather condition every 10 minutes, falling back to cached data if the API is unreachable. WMO weather codes are mapped to human-readable conditions (clear, overcast, rain, fog, etc.).

### web.py — Dashboard

A single-page Flask app with embedded HTML/CSS/JS. Dark-themed, mobile-responsive. Provides:
- Device info, power toggle, brightness/color-temp sliders
- Color presets and custom color picker
- Effects browser
- Sunlight simulator controls (start/stop, location, facing, peak brightness, bias)
- Real-time light preview, phase display, weather status
- Live log viewer
- 24-hour timeline preview

The simulator runs as a background thread with generation-based lifecycle management. Thread safety is ensured via `threading.Lock` for the start endpoint, preventing duplicate loops from concurrent requests.

### config.py — Persistence and Locking

Configuration (device IP, auth token) is stored in `~/.config/nanoleaf-ctl/config.json` using atomic, owner-only writes. An OS-level exclusive lock (`fcntl.flock` on Linux, `msvcrt.locking` on Windows) at `~/.config/nanoleaf-ctl/sunlight.lock` prevents multiple simulator instances on the same machine. The lock file records `hostname:pid` for diagnostics.

### client.py — Device Communication

Thin wrapper around the `nanoleafapi` library. Handles discovery (SSDP), pairing, connection with saved credentials, and color parsing (hex, RGB, named colors).

### cli.py — Command Interface

17 subcommands:

| Command | Purpose |
|---------|---------|
| `discover` | Find devices on the network |
| `pair <ip>` | Authenticate with a device |
| `setup <ip> <token>` | Save config from prior pairing |
| `info` | Show device details |
| `on` / `off` / `toggle` | Power control |
| `brightness <0-100>` | Set brightness |
| `color <color>` | Set color (hex, RGB, or name) |
| `color-temp <1200-6500>` | Set color temperature |
| `effects` / `effect <name>` | List/activate effects |
| `sunlight` | Run the simulator (with `--preview` for dry run) |
| `web` | Start the web dashboard |
| `install` / `uninstall` | Windows auto-start (Task Scheduler) |
| `identify` | Flash panels |
| `forget` | Clear saved config |

## Deployment

On the Raspberry Pi (nanoserver):

- **Installed as**: editable pip package (`pip install -e .`) in a venv at `/home/northfoggy/nanoleaf/venv/`
- **Auto-start**: systemd service `nanoleaf.service` runs `nanoleaf-ctl web --port 5000` at boot
- **Simulator start**: via the web UI (click Start), which spawns a background thread

```
[systemd] → nanoleaf-ctl web --port 5000
                 ↓
           Flask app (port 5000)
                 ↓ (on Start click)
           _run_sim_loop() thread
                 ↓ (every 60s)
           compute → weather → apply → Nanoleaf
```

## Conflict Prevention

Multiple safeguards prevent duplicate controllers from fighting:

1. **Thread-level**: `threading.Lock` around the start endpoint prevents race conditions from concurrent HTTP requests
2. **Process-level**: an OS-level exclusive file lock prevents CLI and web instances from running simultaneously on the same machine
3. **Conflict detection**: Each cycle reads back the device brightness — if it differs from what was last set, a `CONFLICT` warning is logged
4. **Hostname logging**: Lock file and startup logs include `hostname:pid` for cross-machine diagnostics

The systemd watchdog is fed by a dedicated process-liveness thread, independent of simulator state. Stopping the simulator from the dashboard therefore leaves the web service healthy and stopped rather than triggering a watchdog restart.

## Dependencies

| Package | Purpose |
|---------|---------|
| nanoleafapi >= 2.1.2 | Device API and SSDP discovery |
| astral >= 3.2 | Solar position calculations |
| requests >= 2.28 | HTTP client for weather API |
| flask >= 3.0 | Web dashboard |
| Python >= 3.9 | Runtime |
