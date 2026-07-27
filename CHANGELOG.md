# Changelog

This project follows semantic versioning while it remains experimental.

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
