# Security policy

candygoogy-bot can power machines on and off, so security problems matter. Thank
you for taking the time to report one.

## Reporting a vulnerability

Please report it privately, in one of two ways:

- **Preferred:** GitHub's private reporting. On the repository, open the
  **Security** tab and choose **Report a vulnerability**
  ([direct link](https://github.com/candygoogy/candygoogy-bot/security/advisories/new)).
  Only the maintainers see it.
- **Or by email:** **hello@candygoogy.dev**.

Don't open a public issue, pull request or discussion for a vulnerability: that
would expose everyone running the bot before a fix exists.

Helpful to include:

- what an attacker can do, and what they need first (e.g. "anyone who can send
  the bot a message", "a user on the bot's host", "someone on the LAN");
- the steps to reproduce it, or a proof of concept;
- the release and commit you tested (`/about` shows both) and how the bot runs (Docker or
  systemd; the target machines' operating systems);
- anything you've found about a fix.

Please leave real tokens, keys, IP addresses and personal data out of the
report, or replace them with placeholders.

## What to expect

This is a small project maintained in spare time, so responses are best effort.
You can expect an acknowledgement within a few days and an honest assessment
afterwards. Once a fix is released, you'll be credited with it, unless you'd
rather not be.

## Supported versions

| Version | Supported |
|---|---|
| The latest release (1.x) | Yes |
| The latest commit on `main` | Yes |
| Anything older | No |

Please update to one of them before reporting, and check that the problem is
still there. [CHANGELOG.md](CHANGELOG.md) lists what each release changed.

## How the bot is meant to be safe

The README's [Security model](README.md#security-model) describes who can use
the bot, what it runs on your machines, and what it stores. A report showing
that any of it doesn't hold is exactly what this policy is for.
