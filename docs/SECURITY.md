# Security

## Security model

This is a personal, LAN-only home-automation application. It is not designed as
a multi-user or internet-facing service.

Protected assets include:

- the Nanoleaf authentication token;
- the ability to control the lights;
- device metadata such as serial and firmware information;
- the Raspberry Pi account and SSH key;
- location and orientation displayed by the dashboard.

## Network exposure

The web server binds to `0.0.0.0` and the API has no authentication. Anyone who
can reach port 5000 can issue device-control commands.

Required controls:

- keep the Pi and dashboard on a trusted home LAN;
- do not forward port 5000 from the router;
- do not expose it through a public tunnel without adding authentication and
  TLS in a separate reverse proxy;
- use Wi-Fi segmentation carefully, because an untrusted IoT VLAN may not be an
  appropriate place for the unauthenticated dashboard;
- treat browser access as administrative access to the lights.

## Nanoleaf token

The token is stored in `~/.config/nanoleaf-ctl/config.json` because the local
Nanoleaf API requires it. The application uses atomic replacement and mode
`600` for writes.

Safe checks:

```bash
stat -c '%a %U %G %n' "$HOME/.config/nanoleaf-ctl/config.json"
```

Unsafe actions:

- printing the config into shared terminal output;
- committing it to Git;
- placing the token in documentation, screenshots, issues, or chat;
- sharing device URLs containing `/api/v1/<token>/`;
- copying a token into a long-lived shell history via `setup` when pairing is
  available.

Application errors redact credential-bearing URLs and token assignments before
writing simulator logs or returning API errors.

## Filesystem permissions

Expected private paths:

| Path | Mode |
|---|---|
| `~/.config/nanoleaf-ctl/config.json` | `600` |
| `~/.config/nanoleaf-ctl/` | owner-controlled |
| `~/.nanoleaf-ctl/` | `700` |
| `~/.nanoleaf-ctl/sunlight.log` | `600` |
| Dedicated SSH private key | readable only by its Windows account |
| `~/.ssh/authorized_keys` on the Pi | `600` |

The systemd service sets `UMask=0077`, so new service-created files are private
by default.

## Systemd hardening

The included unit applies:

- `User=nanoleaf`: a dedicated, non-root application process;
- `NoNewPrivileges=true`: the process cannot gain privileges;
- `PrivateTmp=true`: a private temporary directory;
- `ProtectSystem=strict`: system paths are read-only;
- `ProtectHome=read-only`: home directories are read-only by default;
- `ReadWritePaths=...`: only application config and log directories are writable;
- an owner-only umask;
- an independent process watchdog.

These settings limit host impact but do not authenticate dashboard users.

## SSH administration

Use a dedicated key rather than sharing a password. Keep its private half
outside the repository, and give its public-key entry a distinctive label such
as `nanoserver-admin` in `authorized_keys`.

To find the public-key entry without displaying unrelated keys:

```bash
grep 'nanoserver-admin' "$HOME/.ssh/authorized_keys"
```

To revoke it, edit `authorized_keys` and remove only that labeled line. Confirm
another administrative login works before ending the existing session.

Do not grant the application service account broad, non-interactive sudo. If
automation requires privileged service management or journal reads, use a
small command allowlist and keep general host administration under a separate
account.

## Logs and diagnostics

Logs can reveal location, device state, network addresses, and—on historical
versions—credential-bearing URLs. Current logging redacts known token formats,
uses owner-only permissions, and rotates at bounded size.

Before sharing logs, pass them through the redaction filter documented in
[Troubleshooting](TROUBLESHOOTING.md) and inspect the result manually.

Quarantined historical logs should remain mode `600`. Delete them when their
diagnostic value is exhausted. Deleting a file does not guarantee forensic
erasure from flash media.

## Dependency and update practices

- Install from the repository virtual environment, not system Python.
- Run tests before restarting production.
- Review dependency changes in `pyproject.toml`.
- Keep the Pi OS and OpenSSH security updates current.
- Use fast-forward-only Git pulls on the Pi.
- Do not run arbitrary installation commands copied from dashboard content or
  logs.

## Repository safeguards

The public GitHub repository uses:

- GitHub secret scanning;
- push protection for detected credentials;
- pull-request CI on Python 3.10 and 3.13;
- ignore rules for common credential, private-key, environment, runtime-log,
  and local configuration files;
- a noreply Git commit address for maintainer privacy.

These safeguards reduce future risk but do not replace review. Run a full
history scanner before publishing any branch that previously held local data.

## Reporting a security issue

Do not include live tokens, private keys, precise private-network details, or
unredacted logs in a public issue. Describe the affected code path and provide a
minimal redacted reproduction.
