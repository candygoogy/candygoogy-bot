# Contributing

Thanks for wanting to help! candygoogy-bot is a small project, so a few clear
rules keep it easy to look after.

## What helps most

- **Reports from real Linux and macOS machines.** Their support is experimental:
  the commands are tested against sample outputs, not yet on many real machines.
  "Sleep works on Fedora 41 with polkit 125" or "`/status` shows the wrong disk on
  macOS 15" is very useful.
- **Bug reports** with clear steps, and the version from `/about`.
- **Small, focused pull requests**: a fix, a clearer message, a missing test, a
  doc that confused you.

For a bigger change (a new command, a new setting, another operating system),
please open a feature request first, so we can agree on the shape before you
spend time on it.

## Development setup

The [Development](README.md#development) section of the README has the setup
and the layout of the code. In short:

```sh
python -m venv venv
venv/bin/python -m pip install --require-hashes -r requirements-dev.txt   # Windows: venv\Scripts\python
```

Before opening a pull request, run all four checks; CI runs the same ones on
Python 3.11, 3.13 and 3.14:

```sh
pytest
ruff check .
ruff format --check .
basedpyright
```

## Dependencies

`requirements.txt` and `requirements-dev.txt` are hash-locked, generated files;
never edit them by hand. Change `requirements.in` or `requirements-dev.in`, then
regenerate both locks:

```sh
uv pip compile requirements.in     --universal --python-version 3.11 --generate-hashes -o requirements.txt
uv pip compile requirements-dev.in --universal --python-version 3.11 --generate-hashes -o requirements-dev.txt
```

Without `--upgrade`, the pins you didn't touch stay as they are. Please keep
dependency updates in their own pull request.

## The one hard rule

**Nothing from a chat may ever reach a command line.** Every command the bot
runs on a machine is a constant for its operating system, and names from chat
are only ever looked up among the configured machines.
`tests/test_subprocess_invariant.py` checks this for every place the bot starts
a program. If a change makes that test fail, the change needs another approach:
please don't loosen the test.

Also: no values in error messages or logs (a setting's name, never its value),
and the bot token stays out of every log line.

## Commits and pull requests

- Small commits with clear messages that say what changed and why.
- Signed commits are welcome.
- Pull requests are squash-merged, so the history stays one commit per change.
- If you add or change a setting, update `.env.example` and the README's
  configuration table too (tests check both against the code).
- For anything a user would notice, add a line under **Unreleased** in
  [CHANGELOG.md](CHANGELOG.md).

## Security problems

Please don't open an issue for a vulnerability. See [SECURITY.md](SECURITY.md).
