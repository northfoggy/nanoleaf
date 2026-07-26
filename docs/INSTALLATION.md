# Installation and deployment

## Supported deployment

The reference production target is a Raspberry Pi Zero 2 W running Linux and
systemd. The portable example uses the dedicated service account `nanoleaf`,
the host name `nanoserver`, and the repository path
`/home/nanoleaf/nanoleaf`.

The Python package itself also runs on Windows and other Linux hosts. The
included systemd unit is path-specific and must be edited for other accounts.

## Clone and install

```bash
git clone https://github.com/OWNER/nanoleaf.git
cd nanoleaf
python3 -m venv venv
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install -e ".[dev]"
venv/bin/python -m pytest -q
```

An editable install is used on the Pi, so pulling Python source updates changes
the installed application without reinstalling the package. Re-run the pip
install when dependencies or project metadata change.

## Discover and pair

The Nanoleaf and server must be on the same LAN.

```bash
venv/bin/nanoleaf-ctl discover
```

Hold the Nanoleaf power button for 5-7 seconds until its pairing indicator
flashes, then run:

```bash
venv/bin/nanoleaf-ctl pair <device-ip>
```

To reuse an existing token instead of pairing:

```bash
venv/bin/nanoleaf-ctl setup <device-ip> <token>
```

Avoid placing a real token in shell history when possible. Pairing is preferred.
Verify the saved file without printing its contents:

```bash
stat -c '%a %U %G %n' "$HOME/.config/nanoleaf-ctl/config.json"
```

Expected mode: `600`.

## Test interactively

```bash
venv/bin/nanoleaf-ctl info
venv/bin/nanoleaf-ctl sunlight --preview
venv/bin/nanoleaf-ctl web --port 5000
```

Open `http://nanoserver:5000/` or use the Pi's LAN address. Stop the interactive
server before enabling systemd so two web processes do not compete for the
port.

## Install the systemd service

Review the deployment-specific paths first:

```bash
grep -E '^(User|WorkingDirectory|ExecStart|ReadWritePaths)=' nanoleaf.service
```

Validate and install the unit:

```bash
sudo systemd-analyze verify "$PWD/nanoleaf.service"
sudo install -o root -g root -m 644 \
  "$PWD/nanoleaf.service" \
  /etc/systemd/system/nanoleaf.service
sudo systemctl daemon-reload
sudo systemctl enable --now nanoleaf.service
```

Verify readiness:

```bash
systemctl show nanoleaf.service \
  -p ActiveState \
  -p SubState \
  -p Result \
  -p MainPID \
  -p NRestarts \
  --no-pager

curl --fail --silent --show-error \
  http://127.0.0.1:5000/api/health
```

Healthy output has `ActiveState=active`, `SubState=running`, a nonzero
`MainPID`, and JSON with `"status":"ok"`.

## Upgrade

Use a fast-forward-only pull so an unexpected local divergence cannot be
silently merged into production:

```bash
cd /home/nanoleaf/nanoleaf
sudo systemctl stop nanoleaf.service
git status -sb
git fetch origin --prune
git pull --ff-only
venv/bin/python -m pip install -e ".[dev]"
venv/bin/python -m pytest -q
sudo systemctl start nanoleaf.service
curl --fail --silent http://127.0.0.1:5000/api/health
```

If `nanoleaf.service` changed, reinstall it and run `daemon-reload` before
starting the service.

## Roll back

Record the currently deployed commit before an upgrade:

```bash
git rev-parse --short HEAD
```

To inspect an older known-good commit without rewriting branch history, create
a temporary recovery branch:

```bash
sudo systemctl stop nanoleaf.service
git switch -c recovery/<date> <known-good-commit>
venv/bin/python -m pytest -q
sudo systemctl start nanoleaf.service
```

Return to the deployment branch after the issue is understood. Do not use
`git reset --hard` on the Pi when local changes or logs have not been reviewed.

## Windows startup

The CLI includes `install` and `uninstall` commands for Windows Task Scheduler:

```powershell
nanoleaf-ctl install --lat 34.13 --lon -84.34 --facing southwest
nanoleaf-ctl uninstall
```

The Raspberry Pi systemd deployment is preferred for continuous service.
