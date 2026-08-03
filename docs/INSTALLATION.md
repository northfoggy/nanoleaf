# Installation and deployment

## Supported deployment

The reference production target is a Raspberry Pi Zero 2 W running Linux and
systemd. The portable example uses the dedicated service account `nanoleaf`,
the host name `nanoserver`, the repository path `/opt/nanoleaf`, and private
runtime state under `/var/lib/nanoleaf`.

The Python package itself also runs on Windows and other Linux hosts. The
included systemd unit is path-specific and must be edited for other accounts.

Before continuing, read [Device safety and third-party notice](../SAFETY.md).
Use Nanoleaf's [official support instructions](https://support.nanoleaf.me/hc/en-us)
for the exact device model. Project instructions do not replace the
manufacturer's installation, pairing, reset, electrical, firmware, or safety
instructions.

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

## Provision the production account

The bundled systemd unit expects a dedicated account and an application-owned
checkout. Create both before pairing or installing the service:

```bash
sudo useradd --system --create-home \
  --home-dir /var/lib/nanoleaf \
  --shell /usr/sbin/nologin \
  nanoleaf
sudo install -d -o nanoleaf -g nanoleaf -m 755 /opt/nanoleaf
sudo -u nanoleaf git clone https://github.com/OWNER/nanoleaf.git /opt/nanoleaf
sudo -u nanoleaf python3 -m venv /opt/nanoleaf/venv
sudo -u nanoleaf /opt/nanoleaf/venv/bin/python -m pip install --upgrade pip
sudo -u nanoleaf /opt/nanoleaf/venv/bin/python -m pip install -e "/opt/nanoleaf[dev]"
sudo -u nanoleaf /opt/nanoleaf/venv/bin/python -m pytest -q /opt/nanoleaf/tests
```

If the account already exists, `useradd` reports that fact; verify its home is
`/var/lib/nanoleaf` before continuing. Do not grant this account sudo access.

## Discover and pair

The Nanoleaf and server must be on the same LAN. For an interactive development
checkout, use the commands below directly. For the production service account,
prefix them with `sudo -u nanoleaf env HOME=/var/lib/nanoleaf` and use the
executables under `/opt/nanoleaf/venv/bin`.

```bash
venv/bin/nanoleaf-ctl discover
```

Immediately before running the command, use Nanoleaf's current instructions for
the exact model to open its third-party or local-API pairing window. Button
combinations and timing differ among products; do not guess or substitute reset
instructions. Start with Nanoleaf's
[official pairing guidance](https://support.nanoleaf.me/hc/en-us/articles/33036154567700-Pairing-Nanoleaf-App-Desktop),
then run:

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

For example, production pairing and verification are:

```bash
sudo -u nanoleaf env HOME=/var/lib/nanoleaf \
  /opt/nanoleaf/venv/bin/nanoleaf-ctl pair <device-ip>
sudo stat -c '%a %U %G %n' \
  /var/lib/nanoleaf/.config/nanoleaf-ctl/config.json
```

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
grep -E '^(User|Group|WorkingDirectory|ExecStart|Environment|StateDirectory)=' \
  nanoleaf.service
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

## Install network resilience and persistent diagnostics

The optional recovery timer checks the Pi's default route, NetworkManager link,
and local gateway, not the Nanoleaf. A gateway is accepted when it replies to
ICMP or has a freshly reachable kernel neighbour entry; stale ARP state is not
treated as proof that routing works. This also prevents an unplugged or updating
light from causing a server reboot. With the supplied defaults, three and six
consecutive two-minute failures reactivate the saved NetworkManager profile.
Eight consecutive failures request a reboot, with a 30-minute persistent
cooldown to bound reboot attempts during a router outage.

Review and install the assets:

```bash
sudo sh -n deploy/nanoleaf-network-recovery
sudo install -o root -g root -m 755 \
  deploy/nanoleaf-network-recovery \
  /usr/local/sbin/nanoleaf-network-recovery
sudo install -o root -g root -m 644 \
  deploy/nanoleaf-network-recovery.service \
  deploy/nanoleaf-network-recovery.timer \
  /etc/systemd/system/

sudo install -d -o root -g root -m 755 \
  /etc/NetworkManager/conf.d \
  /etc/systemd/journald.conf.d
sudo install -o root -g root -m 644 \
  deploy/90-nanoleaf-wifi-powersave.conf \
  /etc/NetworkManager/conf.d/90-nanoleaf-wifi-powersave.conf
sudo install -o root -g root -m 644 \
  deploy/60-nanoleaf-persistent-journal.conf \
  /etc/systemd/journald.conf.d/60-nanoleaf-persistent-journal.conf

sudo systemd-analyze verify \
  /etc/systemd/system/nanoleaf-network-recovery.service \
  /etc/systemd/system/nanoleaf-network-recovery.timer
sudo systemctl daemon-reload
sudo systemctl enable nanoleaf-network-recovery.timer
```

Use a planned reboot to apply the NetworkManager and journald policies without
dropping an active SSH session mid-command. After reconnecting, verify:

```bash
systemctl is-enabled nanoleaf-network-recovery.timer
systemctl is-active nanoleaf-network-recovery.timer
systemctl list-timers nanoleaf-network-recovery.timer --no-pager
systemd-analyze cat-config systemd/journald.conf | grep -E 'Storage=|SystemMaxUse=|MaxRetentionSec='
nmcli -g 802-11-wireless.powersave connection show <active-profile>
journalctl --list-boots --no-pager
```

The NetworkManager value `2` means Wi-Fi power saving is disabled. Persistent
journal history appears after the next boot boundary; the first boot cannot
retroactively recover logs erased by an earlier reboot.

## Upgrade

Use a fast-forward-only pull so an unexpected local divergence cannot be
silently merged into production:

```bash
sudo systemctl stop nanoleaf.service
sudo -u nanoleaf git -C /opt/nanoleaf status -sb
sudo -u nanoleaf git -C /opt/nanoleaf fetch origin --prune
sudo -u nanoleaf git -C /opt/nanoleaf pull --ff-only
sudo -u nanoleaf /opt/nanoleaf/venv/bin/python -m pip install -e "/opt/nanoleaf[dev]"
sudo -u nanoleaf /opt/nanoleaf/venv/bin/python -m pytest -q /opt/nanoleaf/tests
sudo systemctl start nanoleaf.service
curl --fail --silent http://127.0.0.1:5000/api/health
```

If `nanoleaf.service` changed, reinstall it and run `daemon-reload` before
starting the service.

## Roll back

Record the currently deployed commit before an upgrade:

```bash
sudo -u nanoleaf git -C /opt/nanoleaf rev-parse --short HEAD
```

To inspect an older known-good commit without rewriting branch history, create
a temporary recovery branch:

```bash
sudo systemctl stop nanoleaf.service
sudo -u nanoleaf git -C /opt/nanoleaf switch -c recovery/<date> <known-good-commit>
sudo -u nanoleaf /opt/nanoleaf/venv/bin/python -m pytest -q /opt/nanoleaf/tests
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
