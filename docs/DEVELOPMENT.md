# Development

## Environment

```bash
python3 -m venv venv
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install -e ".[dev]"
```

On Windows PowerShell, the equivalent interpreter is typically
`venv\Scripts\python.exe`.

The project requires Python 3.10 or newer. Runtime dependencies are declared in
`pyproject.toml`; pytest is the development extra.

## Test suite

```bash
venv/bin/python -m pytest -q
```

Tests cover:

- CLI command behavior;
- device API wrapper and timeouts;
- atomic configuration and permissions;
- solar phases and weather modulation;
- weather caching;
- web validation, error redaction, manual override, health, branding, systemd
  readiness, and bounded log scrubbing.

Tests should not contact a physical Nanoleaf or the live weather service. Mock
network and device boundaries.

## Code map

Start with these public entry points:

| Change | Primary code | Relevant tests |
|---|---|---|
| CLI argument or command | `nanoleaf_ctl/cli.py` | `tests/test_cli.py` |
| Pairing/device operation | `nanoleaf_ctl/client.py` | `tests/test_client.py` |
| Config or process lock | `nanoleaf_ctl/config.py` | `tests/test_config.py` |
| Solar/color algorithm | `nanoleaf_ctl/sunlight.py` | `tests/test_sunlight.py` |
| Weather mapping/cache | `nanoleaf_ctl/weather.py` | `tests/test_weather.py` |
| Dashboard/API/lifecycle | `nanoleaf_ctl/web.py` | `tests/test_web.py` |
| Pi service sandbox | `nanoleaf.service` | Manual `systemd-analyze verify` |

## Design rules

### Device I/O

- Every device HTTP call must have a finite timeout.
- Transport failures must not reveal token-bearing URLs to API clients.
- Automation should treat device loss as recoverable.
- Avoid multiple sequential device writes when one atomic state update works.

### Concurrency

- Read or modify simulator lifecycle state under `_sim_lock`.
- Recheck state after slow operations such as device connection.
- Acquire the process-level file lock before declaring automation running.
- Use generation checks for thread replacement and shutdown.
- Keep systemd readiness independent of device availability.
- Do not add background threads without a clear shutdown/liveness model.

### Small-device resource use

- Do not read unbounded files into memory.
- Keep logs rotating and owner-only.
- Avoid unbounded queues, lists, thread creation, or retry loops.
- Test service memory on the Pi after lifecycle or logging changes.
- Preserve the health endpoint even when the device is offline.

### Security

- Never log or return the token.
- Keep the dashboard's unauthenticated LAN trust boundary explicit.
- Do not weaken systemd hardening to solve an application bug without first
  identifying the failing path.

## Manual verification

Before deployment:

```bash
python -m pytest -q
git diff --check
```

On the Pi:

```bash
venv/bin/python -m pytest -q
sudo systemd-analyze verify /etc/systemd/system/nanoleaf.service
sudo systemctl restart nanoleaf.service
curl --fail --silent http://127.0.0.1:5000/api/health
curl --fail --silent http://127.0.0.1:5000/api/sunlight/status
```

Then verify:

- one `nanoleaf-ctl` process;
- one port-5000 listener;
- stable RSS over at least one 60-second automation cycle;
- zero unexpected restarts;
- dashboard loads from another LAN device;
- direct control starts manual override;
- Resume returns control to automation;
- diagnostics contain no secret.

## Documentation maintenance

Update documentation in the same change when behavior, defaults, endpoints,
deployment paths, security assumptions, or operational recovery changes.

Canonical locations:

- `README.md`: product overview and short onboarding;
- `ARCHITECTURE.md`: design and state model;
- `docs/INSTALLATION.md`: install/deployment lifecycle;
- `docs/CONFIGURATION.md`: user-configurable behavior;
- `docs/OPERATIONS.md`: production runbook;
- `docs/API.md`: HTTP contract;
- `docs/TROUBLESHOOTING.md`: symptom-driven diagnostics;
- `docs/SECURITY.md`: trust boundary and sensitive-data handling.

Do not create dated copies such as `README-old.md`. Git history is the archive.
Replace obsolete documentation in place or remove it when another canonical
document supersedes it.

## Release checklist

1. Confirm intended changes only with `git status -sb`.
2. Run the complete test suite.
3. Run `git diff --check`.
4. Verify documentation links and commands.
5. Push a feature branch and update the draft pull request.
6. Pull the exact commit onto the Pi.
7. Re-run tests on the Pi.
8. Restart and verify service health, device status, memory, and dashboard.
