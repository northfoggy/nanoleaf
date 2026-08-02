# Operations

## Normal service state

The production service should report active/running with a stable PID and zero
unexpected restarts:

```bash
systemctl show nanoleaf.service \
  -p ActiveState \
  -p SubState \
  -p Result \
  -p MainPID \
  -p NRestarts \
  --no-pager
```

The HTTP health endpoint distinguishes web-process health from device state:

```bash
curl --fail --silent --show-error \
  http://127.0.0.1:5000/api/health
```

Example:

```json
{"device_online":true,"simulator_running":true,"status":"ok"}
```

`status: ok` means the web process is responsive. `device_online: false` means
the Nanoleaf is unreachable; it does not mean systemd should restart the app.

## Service control

```bash
sudo systemctl start nanoleaf.service
sudo systemctl stop nanoleaf.service
sudo systemctl restart nanoleaf.service
sudo systemctl status nanoleaf.service --no-pager
```

The service is enabled at boot. Stopping it manually does not disable it for the
next boot. Use `sudo systemctl disable --now nanoleaf.service` only when boot
startup should also be removed.

## Dashboard operation

Open:

```text
http://nanoserver:5000/
```

The top cards show live device power, brightness, model, firmware, and effect.
The house visualization shows the configured location, window orientation,
weather, current solar phase, color temperature or color mode, and target
brightness.

Use direct controls for temporary changes. When automation is running, they
start a one-hour manual override. Use Resume automation to return control early.
Use Stop only when the simulator itself should stop; the dashboard and systemd
service remain running.

## Logs

Systemd lifecycle and HTTP request logs:

```bash
sudo journalctl -u nanoleaf.service -n 100 --no-pager
sudo journalctl -u nanoleaf.service -f
```

Simulator calculation and device logs:

```bash
sudo tail -n 100 /var/lib/nanoleaf/.nanoleaf-ctl/sunlight.log
sudo ls -lh /var/lib/nanoleaf/.nanoleaf-ctl/sunlight.log*
```

The dashboard Diagnostics control exposes the latest 200 in-memory simulator
lines. The persistent file rotates at 1 MB with three backups.

Logs are redacted by the application, but use the redaction pipeline in
[Troubleshooting](TROUBLESHOOTING.md) before sharing any output.

System journals are configured for persistent storage with a 64 MB cap and a
14-day retention limit when the supplied journald drop-in is installed:

```bash
journalctl --list-boots --no-pager
journalctl --disk-usage --no-pager
```

## Network recovery

Inspect the gateway recovery timer without manually invoking recovery:

```bash
systemctl status nanoleaf-network-recovery.timer --no-pager
systemctl list-timers nanoleaf-network-recovery.timer --no-pager
sudo journalctl -u nanoleaf-network-recovery.service --since today --no-pager
```

The timer requires a default route and connected NetworkManager link, then
accepts either an ICMP response or a usable kernel neighbour entry from the
gateway. This avoids treating an ICMP-filtering router as offline. It does not
use Nanoleaf reachability as a reboot condition. A manual test of the service is
safe only while the gateway is reachable; otherwise it increments the real
recovery counter. The counter persists between timer invocations in `/run` and
resets naturally when the Pi boots.

## Verify a single server

```bash
ps -C nanoleaf-ctl -o pid=,ppid=,rss=,nlwp=,stat=,cmd=
sudo ss -ltnp 'sport = :5000'
systemctl list-unit-files --no-pager | grep -i nanoleaf
```

Expected production state:

- one `nanoleaf-ctl web --port 5000` process;
- one listener owned by that PID;
- one enabled `nanoleaf.service` unit.

The local file lock prevents two simulators on the same machine, but two web
servers can still fight for the port before the second one reaches automation.
Use systemd as the only production launcher.

## Resource checks on a Pi Zero 2 W

```bash
free -h
pid="$(systemctl show nanoleaf.service -p MainPID --value)"
ps -o pid=,etimes=,rss=,vsz=,nlwp=,pcpu=,stat=,comm= -p "$pid"
sudo ls -lh /var/lib/nanoleaf/.nanoleaf-ctl/sunlight.log*
```

A normal steady-state process has historically used roughly 40 MB RSS. Treat
rapid growth into hundreds of megabytes or a continuously growing active log as
abnormal. Consult the OOM section in [Troubleshooting](TROUBLESHOOTING.md).

## Update checklist

1. Confirm the working tree is clean with `git status -sb`.
2. Record the current commit with `git log -1 --oneline`.
3. Stop the service.
4. Pull with `git pull --ff-only`.
5. Refresh the editable install if dependencies changed.
6. Run all tests.
7. Reinstall the systemd unit if it changed.
8. Start the service.
9. Check `/api/health`, simulator status, memory, and restart count.
10. Load the dashboard from another LAN device.
11. Confirm the network recovery timer is active and persistent journal usage
    remains bounded.

## Backup and recovery

The only irreplaceable runtime data is the Nanoleaf pairing token. Back up
`config.json` to encrypted storage without printing it to the terminal.

Source code should be recovered from GitHub rather than backed up from the Pi.
Logs are diagnostic and disposable. Lock files should never be restored from a
backup.

## SSH access

Connect as the deployment host's designated administrator using a dedicated
SSH key. Keep private keys outside the repository. To revoke a key, remove only
its matching public-key line from `~/.ssh/authorized_keys`, then test a separate
working login before closing the administrative session.
