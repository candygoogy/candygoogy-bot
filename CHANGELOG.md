# Changelog

All notable changes to candygoogy-bot are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.2.0] - 2026-10-13

### Added

- Repeating schedules: `/schedule_shutdown weekdays 23:00` or
  `/schedule_wake mon,wed,fri 07:30` repeat every week, with a warning before
  a shutdown (skip this one, or +30m), skip-next and delete in `/timers`, and
  missed runs reported, never run late.

## [1.1.0] - 2026-10-12

### Added

- Published images, `ghcr.io/candygoogy/candygoogy-bot`, for amd64 and arm64,
  with a signed build provenance attestation. `docker-compose.yml` now pulls
  the image; `docker-compose.build.yml` builds it from the checkout instead.
- The host's CPU temperature in `/homelab`, from its thermal zones or CPU
  sensor, with an optional alert (`HOMELAB_TEMP_ALERT_C`).
- `/about` shows the release as well as the commit it's running.
- Python 3.14 support; the image now runs on Python 3.14.

### Fixed

- The "when?" prompt of `/schedule_shutdown` and `/schedule_wake` is per user:
  with two allowed users, one user's typed time no longer answers the other's
  prompt, and one user's taps no longer close it.

## [1.0.0] - 2026-10-11

The first public release.

### Added

- Wake-on-LAN with a real follow-up: "Booted in 45s" or "Woke from sleep in 4s".
- Sleep, reboot and shut down over SSH, each after a confirmation, then checked.
- Timers: shut down at 22:30, wake in 8 hours, with a warning before a shutdown.
- Several machines: Windows, and Linux and macOS (experimental), picked by name
  or with a button.
- Homelab health in `/homelab`: disks, failed services, Docker containers,
  updates, pending reboots and backups, with alerts when something goes wrong
  and when it clears.
- Internet outages: noticed, timed and reported when the connection is back.
- A daily summary and a weekly report, sent at times you choose.
- Logs in the chat: recent lines, or everything as a file.
- Only allowlisted users, in a private chat, can use the bot.
- Runs as a locked-down Docker container or a hardened systemd service.

[Unreleased]: https://github.com/candygoogy/candygoogy-bot/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/candygoogy/candygoogy-bot/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/candygoogy/candygoogy-bot/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/candygoogy/candygoogy-bot/releases/tag/v1.0.0
