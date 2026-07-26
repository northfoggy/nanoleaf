# NanoLeaf Sunlight Simulator

**NanoLeaf Sunlight Simulator by Quicksilver Industries LTD.** is a local-first
controller for Nanoleaf lighting. It combines direct device controls with a
weather-aware automation engine that makes indoor panels follow the daylight
that would arrive through a real window.

The application can run from the command line or as a browser dashboard. Its
primary deployment target is a Raspberry Pi on the same LAN as the Nanoleaf
device.

## What it does

- Controls power, brightness, color, color temperature, and saved effects.
- Computes the sun's elevation and azimuth for a configured location.
- Adjusts brightness for the window's compass orientation.
- Uses blue-hour, golden-hour, daylight, and nighttime lighting profiles.
- Modulates the result using current cloud cover and weather from Open-Meteo.
- Applies smooth, one-minute transitions to the Nanoleaf device.
- Automatically resumes the correct state after a device disconnect.
- Pauses automation for one hour when a person makes a direct dashboard change.
- Prevents two simulator processes on the same host from controlling the lights.
- Exposes a responsive LAN dashboard with a live house, location, orientation,
  weather, and simulated-light visualization.

## Requirements

- Python 3.10 or newer
- A Nanoleaf device on the same local network
- Internet access for live weather (optional)
- Linux with systemd for the documented Raspberry Pi service deployment

## Quick start

Create a virtual environment and install the project:

```bash
git clone https://github.com/northfoggy/nanoleaf.git
cd nanoleaf
python3 -m venv venv
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install -e ".[dev]"
```

Find and pair with the Nanoleaf:

```bash
venv/bin/nanoleaf-ctl discover
venv/bin/nanoleaf-ctl pair <device-ip>
```

Hold the Nanoleaf power button for 5-7 seconds before running `pair`. Pairing
stores the device IP and token in
`~/.config/nanoleaf-ctl/config.json` with owner-only permissions.

Start the dashboard:

```bash
venv/bin/nanoleaf-ctl web --port 5000
```

Then open `http://<server-name-or-ip>:5000/` from another device on the LAN.
The web process automatically starts the sunlight automation using the defaults
in `nanoleaf_ctl/sunlight.py`.

## Common commands

```bash
nanoleaf-ctl info
nanoleaf-ctl on
nanoleaf-ctl off
nanoleaf-ctl brightness 60
nanoleaf-ctl color "#ffd29b"
nanoleaf-ctl color-temp 4000
nanoleaf-ctl effects
nanoleaf-ctl effect "Northern Lights"
nanoleaf-ctl sunlight --preview
nanoleaf-ctl sunlight --lat 34.13 --lon -84.34 --facing southwest
```

Run `nanoleaf-ctl <command> --help` for the complete arguments accepted by a
command.

## Raspberry Pi service

The included `nanoleaf.service` is configured for the current deployment:

- user: `northfoggy`
- checkout: `/home/northfoggy/nanoleaf`
- virtual environment: `/home/northfoggy/nanoleaf/venv`
- dashboard: port `5000`

If those paths differ on another machine, edit the unit before installing it.
See [Installation and deployment](docs/INSTALLATION.md) for the complete,
verified procedure.

## Documentation

| Document | Purpose |
|---|---|
| [Architecture](ARCHITECTURE.md) | Components, data flow, state, concurrency, and reliability model |
| [Installation and deployment](docs/INSTALLATION.md) | Development install, pairing, Raspberry Pi, systemd, upgrades, and rollback |
| [Configuration](docs/CONFIGURATION.md) | Device credentials, sunlight parameters, defaults, and runtime behavior |
| [Operations](docs/OPERATIONS.md) | Daily operation, health checks, logs, updates, and recovery |
| [HTTP API](docs/API.md) | Dashboard API endpoints, payloads, responses, and trust boundary |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | Diagnostic procedures for service, device, network, weather, and memory issues |
| [Security](docs/SECURITY.md) | Token handling, permissions, LAN exposure, service hardening, and SSH access |
| [Development](docs/DEVELOPMENT.md) | Test workflow, code map, change checklist, and release verification |
| [Brand policy](TRADEMARKS.md) | Permitted references to Quicksilver Industries and restrictions on branding forks |

## Important security note

The dashboard and HTTP API do not authenticate users. They are intended only
for a trusted home LAN. Do not expose port 5000 directly to the public internet.
The Nanoleaf token is equivalent to a device password; never paste the config
file or an unredacted device URL into an issue or chat.

## Tests

```bash
venv/bin/python -m pytest -q
```

## Brand policy

The MIT License covers the software, but it does not grant permission to brand
forks, modified versions, products, or services as **Quicksilver Industries**
or **Quicksilver Industries LTD.**, or to imply endorsement or affiliation.
Factual references to the project's origin are permitted. See
[Brand and trademark policy](TRADEMARKS.md).

## License

MIT. See [LICENSE](LICENSE).
