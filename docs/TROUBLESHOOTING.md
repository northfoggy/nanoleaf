# Troubleshooting

## Safe first response

When the service is repeatedly restarting or the Pi is becoming unresponsive,
stop the loop before collecting extensive diagnostics:

```bash
sudo systemctl stop nanoleaf.service
systemctl is-active nanoleaf.service
```

Expected result: `inactive`.

Do not repeatedly start a service that is invoking the kernel OOM killer.

## Service does not start

Collect state and recent logs:

```bash
systemctl show nanoleaf.service \
  -p ActiveState \
  -p SubState \
  -p Result \
  -p MainPID \
  -p ExecMainCode \
  -p ExecMainStatus \
  -p NRestarts \
  --no-pager

sudo journalctl -u nanoleaf.service -n 80 --no-pager -o short-iso
```

Common interpretations:

| Signal | Likely cause |
|---|---|
| `Result=exit-code` | Python exception, CLI error, missing dependency, or invalid path |
| `Result=signal`, status `9/KILL` | External kill; inspect kernel OOM logs |
| `activating` until timeout | Readiness notification was not received |
| Rapid `NRestarts` growth | Crash/restart loop; stop before continuing |
| Permission denied under home/config paths | systemd hardening path or ownership mismatch |

Verify the unit and executable:

```bash
sudo systemd-analyze verify /etc/systemd/system/nanoleaf.service
test -x /opt/nanoleaf/venv/bin/nanoleaf-ctl
sudo stat -c '%a %U %G %n' \
  /var/lib/nanoleaf/.config/nanoleaf-ctl \
  /var/lib/nanoleaf/.nanoleaf-ctl
```

## Dashboard cannot connect

```bash
systemctl is-active nanoleaf.service
sudo ss -ltnp 'sport = :5000'
curl --fail --silent --show-error http://127.0.0.1:5000/api/health
hostname -I
```

If localhost works but another LAN device does not:

- confirm the client and Pi are on the same network;
- try `http://<pi-ip>:5000/` instead of the hostname;
- verify local DNS resolves `nanoserver`;
- inspect host firewall rules;
- check that guest Wi-Fi client isolation is not enabled.

## Device offline

The web service should remain healthy when the Nanoleaf is unavailable.

```bash
curl --silent http://127.0.0.1:5000/api/health
curl --silent http://127.0.0.1:5000/api/sunlight/status
```

Check:

1. The Nanoleaf has power.
2. The saved IP still belongs to that device.
3. The Pi can reach device port 16021.
4. The token remains valid.
5. Another controller is not saturating the device API.

Do not print `config.json`. To verify its IP without revealing the token, use a
small local parser and output only the `ip` field, or inspect it privately.

## Duplicate controller or server

```bash
ps -C nanoleaf-ctl -o pid=,ppid=,rss=,nlwp=,stat=,cmd=
sudo ss -ltnp 'sport = :5000'
systemctl list-unit-files --no-pager | grep -i nanoleaf
systemctl --user list-unit-files --no-pager 2>/dev/null | grep -i nanoleaf
crontab -l 2>/dev/null
screen -ls 2>/dev/null
tmux list-sessions 2>/dev/null
```

The intended production state is one system service and one web process. The
lock file can identify a local simulator holder:

```bash
sudo cat /var/lib/nanoleaf/.config/nanoleaf-ctl/sunlight.lock
```

The lock contains only `hostname:pid`, not the token.

## Out-of-memory kills

Confirm the kernel's reason before changing systemd timeouts:

```bash
sudo journalctl -k --since '30 minutes ago' --no-pager |
grep -Ei 'oom|out of memory|killed process'
```

Check current memory and logs:

```bash
free -h
sudo ls -lh /var/lib/nanoleaf/.nanoleaf-ctl/sunlight.log*
pid="$(systemctl show nanoleaf.service -p MainPID --value)"
ps -o pid=,etimes=,rss=,vsz=,nlwp=,pcpu=,stat=,comm= -p "$pid"
```

The application now bounds the active log and scrubs it with at most 1 MB in
memory. If an older deployment left a very large file, stop the service and move
the file to a private quarantine location before upgrading:

```bash
sudo mv /var/lib/nanoleaf/.nanoleaf-ctl/sunlight.log \
  /var/lib/nanoleaf/.nanoleaf-ctl/sunlight.log.quarantine
sudo chmod 600 /var/lib/nanoleaf/.nanoleaf-ctl/sunlight.log.quarantine
```

Do not delete a quarantined log until deciding whether its diagnostic history
is needed. It may contain historical sensitive device URLs.

## Weather unavailable or stale

Solar simulation does not require weather. The dashboard can show weather as
unavailable while continuing to control the light.

Check internet reachability and system time. Open-Meteo requests use a
ten-second timeout, and cached data refreshes every ten minutes. `--no-weather`
isolates weather from a direct CLI test.

## Manual controls appear to be undone

When the dashboard is the source of a change, it should show Manual until a
specific time. If another app or CLI changes the device, the web process cannot
automatically know the user's intent; it only detects a brightness conflict.

Options:

- make the change from this dashboard;
- select Stop before using another controller;
- select Resume when the simulator should regain control;
- ensure another host is not running its own simulator.

## Redact logs before sharing

Use this pipeline for journal or log output:

```bash
sed -E \
  -e 's#(https?://[^ ]+/api/v1/)[^/ ]+#\1[REDACTED]#g' \
  -e 's#(auth[_ -]?token[=: ]+)[^,; ]+#\1[REDACTED]#Ig'
```

Never share `config.json`, SSH private keys, or an unredacted URL containing
`/api/v1/<token>/`.

## Full diagnostic bundle (terminal output only)

```bash
echo '=== VERSION ==='
sudo -u nanoleaf git -C /opt/nanoleaf log -1 --oneline
sudo -u nanoleaf /opt/nanoleaf/venv/bin/python --version

echo '=== SERVICE ==='
systemctl show nanoleaf.service \
  -p ActiveState -p SubState -p Result -p MainPID -p NRestarts --no-pager

echo '=== PROCESS ==='
ps -C nanoleaf-ctl -o pid=,ppid=,rss=,nlwp=,stat=,cmd=

echo '=== PORT ==='
sudo ss -ltnp 'sport = :5000'

echo '=== HEALTH ==='
curl --fail --silent --show-error http://127.0.0.1:5000/api/health

echo '=== MEMORY ==='
free -h

echo '=== LOG FILES ==='
sudo ls -lh /var/lib/nanoleaf/.nanoleaf-ctl/sunlight.log*
```
