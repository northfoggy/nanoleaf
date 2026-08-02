# HTTP API

## Scope and trust model

The Flask API powers the bundled dashboard and is also usable by other trusted
LAN clients. It listens on all interfaces when started with the default web
command.

There is no API authentication, authorization, CSRF protection, or TLS. Do not
port-forward it, publish it through a public tunnel, or expose it directly to an
untrusted network. See [Security](SECURITY.md).

The default base URL on the Raspberry Pi is:

```text
http://nanoserver:5000
```

JSON request bodies require `Content-Type: application/json`.

## Health and status

### `GET /api/health`

Process-level health probe. It does not contact the device.

```json
{
  "status": "ok",
  "simulator_running": true,
  "device_online": true
}
```

Use this route for system monitoring. HTTP 200 with `device_online: false`
means the web service is healthy while the Nanoleaf is unreachable.

### `GET /api/sunlight/status`

Returns the automation state, control mode, device reachability, current
configuration, and latest computed light state.

Representative response:

```json
{
  "running": true,
  "demo": false,
  "device_online": true,
  "control_mode": "automation",
  "manual_override_until": null,
  "nap": null,
  "device_last_seen": 1785100301.17,
  "config": {
    "latitude": 34.13,
    "longitude": -84.34,
    "timezone": "America/New_York",
    "facing": "southwest",
    "peak_brightness": 75,
    "brightness_bias": -5
  },
  "state": {
    "phase": "midday",
    "mode": "color_temp",
    "color_temp": 5530,
    "brightness": 68,
    "weather": "clear",
    "cloud_cover": 6
  }
}
```

Optional fields are omitted until a state or configuration exists. Epoch values
are Unix seconds.

## Device information and control

### `GET /api/info`

Contacts the Nanoleaf and returns its current summary:

```json
{
  "name": "Skylight",
  "model": "NL64",
  "serial": "...",
  "firmware": "12.3.4",
  "num_panels": 15,
  "power": true,
  "brightness": 68,
  "color_temp": 5530,
  "effect": "*Solid*"
}
```

### `POST /api/power`

Payload:

```json
{"action":"on"}
```

`action` may be `on`, `off`, or `toggle`. Unknown values currently behave as
`toggle`. Response: `{"power":"on"}` or `{"power":"off"}`.

### `POST /api/brightness`

Payload: `{"level":42}`. Values are converted to integers and clamped to
0-100. Response: `{"brightness":42}`.

### `POST /api/color`

Payload: `{"color":"#ffb464"}`. Named colors, hex values, and supported RGB
text forms use the same parser as the CLI. Invalid colors return HTTP 400.

### `POST /api/color-temp`

Payload: `{"temp":4000}`. Values are converted to integers and clamped to
1200-6500 K.

### `GET /api/effects`

Returns sorted effect names and the selected effect:

```json
{"effects":["Fireplace","Golden Hour"],"current":"Golden Hour"}
```

### `POST /api/effect`

Payload: `{"name":"Golden Hour"}`. The name must match a device effect
exactly. Unknown effects return HTTP 404.

All successful direct-control routes start a one-hour manual override when the
simulator is running.

## Nap Mode

### `POST /api/nap/start`

Both fields are optional:

```json
{"minutes":40,"brightness":5}
```

Duration is clamped to 5-180 minutes and brightness to 1-20%. The route applies
a warm amber RGB scene, pauses daylight writes, and returns the scheduled Unix
end time:

```json
{"status":"nap started","minutes":40,"brightness":5,"until":1785102701.17}
```

`GET /api/sunlight/status` reports `control_mode: "nap"` and a `nap` object
containing `until`, `brightness`, and `rgb` while active. Returns HTTP 409 when
sunlight automation is stopped and HTTP 502 if the device cannot be reached.

### `POST /api/nap/stop`

No body is required. Ends Nap Mode early and forces the current daylight state
to be reapplied. If Nap Mode is not active, returns
`{"status":"not active"}` without changing the current control mode.

## Automation control

### `POST /api/sunlight/start`

All fields are optional:

```json
{
  "lat": 34.13,
  "lon": -84.34,
  "tz": "America/New_York",
  "facing": "southwest",
  "peak": 75,
  "bias": -5,
  "night_glow": false,
  "no_weather": false,
  "demo": false
}
```

Responses:

- HTTP 200 `{"status":"started"}`;
- HTTP 200 `{"status":"demo started"}`;
- HTTP 200 `{"status":"already running"}`;
- HTTP 400 for invalid configuration;
- HTTP 409 if another local simulator holds the process lock;
- HTTP 502 if device connection fails.

Demo mode advances 15 simulated minutes per tick and completes a full day in
approximately eight minutes. It omits live weather.

### `POST /api/sunlight/stop`

No body is required. Stops automation and clears any manual override. The web
service remains available. Response: `{"status":"stopped"}`.

### `POST /api/sunlight/resume`

Ends a manual override and forces state reconciliation on the next automation
cycle. Returns HTTP 409 when the simulator is stopped.

### `GET /api/sunlight/preview`

Query parameters: `lat`, `lon`, `tz`, `facing`, `peak`, and `bias`.

Returns 48 computed states at 30-minute intervals for the current local day.
The preview performs solar calculation only and does not control the device.

### `GET /api/sunlight/log`

Returns the latest 200 in-memory simulator messages:

```json
{"lines":["[17:10:01] Computed: ..."]}
```

## Error behavior

Device transport errors are logged internally and returned as a generic HTTP
502 response so token-bearing URLs are not exposed:

```json
{"error":"Unable to read device status; check device connectivity"}
```

Validation errors use HTTP 400. State conflicts use HTTP 409. Missing effects
use HTTP 404.

## Examples

```bash
curl --fail --silent http://nanoserver:5000/api/health

curl --fail --silent \
  -H 'Content-Type: application/json' \
  -d '{"level":50}' \
  http://nanoserver:5000/api/brightness

curl --fail --silent \
  -X POST \
  http://nanoserver:5000/api/sunlight/resume
```
