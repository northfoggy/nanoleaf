# Changelog

This project follows semantic versioning while it remains experimental.

## 0.3.0

### Added

- Nap Mode, which applies a dim warm-amber scene for a configurable duration
  and then hands control back to daylight automation automatically.
- Dashboard controls and live status for starting a nap, seeing its scheduled
  end, and returning to automation early.

### Changed

- Timed overrides are checked once per second so the post-nap daylight state is
  restored promptly instead of waiting for the next minute-long cycle.

## 0.2.2

### Fixed

- Network recovery now verifies the NetworkManager link and accepts a usable
  kernel neighbour entry when a working gateway intentionally rejects ICMP.
  This prevents false Wi-Fi reconnects and guarded reboots based only on a
  failed ping.
- The oneshot recovery unit now preserves its runtime directory so consecutive
  failure counts survive between timer invocations within the current boot.

## 0.2.1

This reliability and security release addresses a sustained Raspberry Pi Wi-Fi
route failure observed in production.

### Added

- A gateway-scoped systemd recovery timer that reconnects Wi-Fi after repeated
  failures and requests a guarded reboot only after a longer outage.
- Deployment assets for disabling NetworkManager Wi-Fi power saving and keeping
  a size- and age-limited persistent system journal across reboots.
- Operational documentation for post-reboot network incident investigation and
  recovery verification.

### Security

- Redaction now removes Nanoleaf tokens from relative `/api/v1/<token>/` error
  paths as well as complete URLs.
- The active simulator log and all three bounded rotations are scrubbed when
  file logging starts.

## 0.2.0

This release turns the original command-line controller into an experimental,
weather-aware sunlight simulator suitable for a continuously running Raspberry
Pi deployment.

### Added

- Weather-aware sunlight automation using solar position, window orientation,
  cloud cover, daylight phase, brightness, and color temperature.
- Responsive LAN dashboard with device controls, effects, diagnostics, manual
  override handling, day preview, and a live house visualization.
- Raspberry Pi and systemd deployment documentation, watchdog integration,
  private state management, single-process locking, and health endpoints.
- Architecture, API, configuration, operations, troubleshooting, security,
  safety, trademark, installation, and screenshot documentation.

### Changed

- Device operations now use bounded timeouts and thread-safe controller state.
- Automation recovers from device disconnections and pauses for intentional
  manual changes, including during accelerated demos.
- Packaging metadata identifies Quicksilver Industries LTD. as the author.

### Security

- Device tokens are stored with owner-only permissions and are no longer
  printed after pairing.
- Credential-bearing errors and URLs are redacted before logging or returning
  API failures.
- The reference service runs as a dedicated non-root account with private,
  systemd-managed state and filesystem hardening.

## 0.1.0

Initial command-line release for discovering, pairing, and controlling
Nanoleaf devices over a local network.
