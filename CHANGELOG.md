# Changelog

All notable changes to candygoogy-bot are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - UNRELEASED

The first versioned release.

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
- `/about` shows the release and the commit it's running.
- Only allowlisted users, in a private chat, can use the bot.
- Runs as a locked-down Docker container or a hardened systemd service.
- Published images, `ghcr.io/candygoogy/candygoogy-bot`, for amd64 and arm64,
  with a signed build provenance attestation.

### Fixed

- The "when?" prompt of `/schedule_shutdown` and `/schedule_wake` is per user:
  with two allowed users, one user's typed time no longer answers the other's
  prompt, and one user's taps no longer close it.

[Unreleased]: https://github.com/candygoogy/candygoogy-bot/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/candygoogy/candygoogy-bot/releases/tag/v1.0.0
