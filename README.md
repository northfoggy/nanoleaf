# nanoleaf-ctl

A command-line and web-based tool for controlling Nanoleaf light panels over your local network. Its headline feature is a **window light simulator** that drives your ceiling-mounted Nanoleaf Skylights through a full day of realistic sunlight — golden hour warmth at dawn, cool white at midday, deep blue at dusk — adjusted in real time for your location, window orientation, and local weather.

## Features

- **Device control** — power, brightness, color, color temperature, and effects from the terminal or a phone-friendly web dashboard
- **Window light simulator** — uses real solar position math (`astral`) to reproduce the color and intensity of light coming through a window throughout the day
- **Weather-aware** — cloud cover and conditions from the free Open-Meteo API dim and cool the light just like an overcast sky would
- **Window orientation** — specify which direction your window faces (N/NE/E/SE/S/SW/W/NW) and the simulator adjusts for how directly the sun hits it
- **Network discovery** — auto-detects Nanoleaf devices via SSDP
- **Web dashboard** — responsive single-page UI with sliders, color picker, effect selector, and full simulator controls accessible from any device on your network
- **Resilient** — gracefully handles the device being power-cycled and re-applies the correct state when it comes back

## Requirements

- Python 3.9+
- A Nanoleaf device (Skylights, Light Panels, Shapes, Essentials) on the same local network
- Internet connection (optional, only for weather integration)

## Installation

```bash
pip install .
```

Or for development:

```bash
pip install -e .
```

## Quick start

### 1. Find your device

```bash
nanoleaf-ctl discover
```

### 2. Pair

Hold the power button on your Nanoleaf for 5–7 seconds until the LEDs start flashing, then:

```bash
nanoleaf-ctl pair <ip>
```

The auth token is saved to `~/.config/nanoleaf-ctl/config.json` — you only need to pair once.

### 3. Control

```bash
nanoleaf-ctl on
nanoleaf-ctl brightness 60
nanoleaf-ctl color orange
nanoleaf-ctl color-temp 4000
nanoleaf-ctl effects            # list available effects
nanoleaf-ctl effect "Northern Lights"
```

### 4. Start the sunlight simulator

```bash
nanoleaf-ctl sunlight --lat 40.7 --lon -74.0 --facing south
```

Preview the full day schedule without sending anything to the device:

```bash
nanoleaf-ctl sunlight --preview --lat 40.7 --lon -74.0 --facing south
```

### 5. Launch the web dashboard

```bash
nanoleaf-ctl web
```

Open `http://<your-ip>:5000` on your phone or laptop.

## Sunlight simulator

The simulator maps solar elevation to distinct lighting phases:

| Solar elevation | Phase | Light |
|---|---|---|
| Below -18° | Night | Off (or dim warm glow with `--night-glow`) |
| -18° to -6° | Twilight | Dark blue, very dim |
| -6° to 0° | Blue hour | Deep blue-purple |
| 0° to 6° | Golden hour | Warm orange/salmon |
| 6° to 40° | Day | Color temperature ramps from 2700 K to 5500 K |
| Above 40° | Midday | Cool neutral white at peak brightness |

Transitions fade over 60 seconds to avoid abrupt changes.

### Simulator options

```
--lat, --lon       Your coordinates (default: Atlanta, GA)
--tz               Timezone (default: America/New_York)
--facing           Window direction: n, ne, e, se, s, sw, w, nw (default: sw)
--peak             Max brightness percentage (default: 75)
--night-glow       Keep a dim warm light at night instead of turning off
--no-weather       Disable weather-based adjustments
--interval         Update frequency in seconds (default: 60)
```

## Web dashboard

The dashboard provides:

- Power toggle, brightness and color temperature sliders
- Color presets, a color picker, and hex/RGB text input
- Effect browser
- Full simulator controls: start/stop, configure location and orientation, live status with current phase and weather, and a 24-hour preview timeline

It runs on Flask and is designed to work from a Raspberry Pi on your home network.

## CLI reference

```
nanoleaf-ctl discover            Find devices on the network
nanoleaf-ctl pair <ip>           Pair with a device
nanoleaf-ctl setup <ip> <token>  Save an existing auth token
nanoleaf-ctl info                Show device details
nanoleaf-ctl on / off / toggle   Power control
nanoleaf-ctl brightness <0-100>  Set brightness
nanoleaf-ctl color <color>       Set color (hex, RGB, or name)
nanoleaf-ctl color-temp <K>      Set color temperature (1200–6500 K)
nanoleaf-ctl effects             List effects
nanoleaf-ctl effect <name>       Activate an effect
nanoleaf-ctl identify            Flash panels for identification
nanoleaf-ctl sunlight [options]  Run the window light simulator
nanoleaf-ctl web [--port PORT]   Start the web dashboard
nanoleaf-ctl forget              Clear saved configuration
```

## License

MIT
