# Configuration

## Device credentials

The persistent device configuration is stored at:

```text
~/.config/nanoleaf-ctl/config.json
```

Schema:

```json
{
  "ip": "192.168.x.x",
  "auth_token": "device-issued-token"
}
```

The application writes this file atomically and sets mode `600`. Treat the
token as a device password. Documentation and diagnostics should show only the
path and permissions, never the file contents.

Commands that modify credentials:

```bash
nanoleaf-ctl pair <device-ip>
nanoleaf-ctl setup <device-ip> <token>
nanoleaf-ctl forget
```

`forget` deletes the saved configuration. It does not revoke the token on the
Nanoleaf device.

## Sunlight defaults

The current defaults are intentionally configured for the owner's installation:

| Setting | Default |
|---|---|
| Latitude | `34.13` |
| Longitude | `-84.34` |
| Timezone | `America/New_York` |
| Window facing | `southwest` |
| Peak brightness | `75` percent |
| Web auto-start bias | `-5` percentage points |
| Normal update interval | `60` seconds |
| Weather cache interval | `10` minutes |
| Manual override | `60` minutes |
| Device request timeout | `10` seconds |

The CLI can override location, timezone, facing, peak brightness, nighttime
behavior, weather use, update interval, and logging for a direct `sunlight`
run:

```bash
nanoleaf-ctl sunlight \
  --lat 34.13 \
  --lon -84.34 \
  --tz America/New_York \
  --facing southwest \
  --peak 75 \
  --interval 60
```

Additional switches:

- `--night-glow`: retain a dim warm light instead of turning off at night;
- `--no-weather`: use solar calculations without Open-Meteo;
- `--preview`: print the 48-point day preview without controlling the device;
- `--log FILE`: write direct CLI simulator output to a selected file.

Accepted facing values are full or abbreviated compass directions:
`north/n`, `northeast/ne`, `east/e`, `southeast/se`, `south/s`,
`southwest/sw`, `west/w`, and `northwest/nw`.

## Dashboard configuration

The dashboard's Start form accepts:

- latitude from -90 to 90;
- longitude from -180 to 180;
- a valid IANA timezone;
- one of the eight window directions;
- peak brightness from 1 to 100 percent;
- brightness bias from -50 to +50 percentage points;
- nighttime glow on/off;
- weather on/off;
- normal or time-lapse demo mode.

Dashboard form values configure the current process only. They are not written
to `config.json`. A service restart returns to the source defaults used by web
auto-start.

## Automatic startup behavior

The web command binds the server and announces systemd readiness before device
connection. It then starts automation in a background worker with the default
location, orientation, peak, and a `-5` brightness bias.

If device connection fails, the dashboard can remain available. A later Start
request retries connection. Only one simulator may hold the local lock.

## Manual override

Changing power, brightness, color, color temperature, or an effect through the
dashboard begins a one-hour manual override when automation is running.

During an override:

- the simulator continues to calculate desired daylight;
- it does not send automatic updates to the Nanoleaf;
- a time-lapse demo pauses its simulated clock until automation resumes;
- the dashboard shows the override expiration time;
- Resume ends the override and forces automation to reconcile;
- Stop ends both automation and the override.

Direct CLI commands and third-party controllers are not able to set the web
process's override state. Brightness readback provides best-effort conflict
detection for those controllers.

## Runtime files

| File | Role |
|---|---|
| `~/.config/nanoleaf-ctl/sunlight.lock` | Exclusive local simulator lock and holder metadata |
| `~/.nanoleaf-ctl/sunlight.log` | Current persistent simulator log |
| `~/.nanoleaf-ctl/sunlight.log.1` through `.3` | Rotated log history |

The active log rotates at 1 MB. Startup redaction retains at most 1 MB from a
pre-existing active log, preventing unbounded memory use on small hardware.
The bundled systemd service sets `HOME=/var/lib/nanoleaf`, so these `~` paths
resolve beneath `/var/lib/nanoleaf` in the reference production deployment.
