"""Entry point: `python bot.py`. Loads .env, checks the configuration, runs the bot.

`python bot.py --check-config` only checks the configuration: it prints OK or
the problems, and exits 0 or 2 without starting the bot or contacting Telegram.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from candygoogy.settings import ConfigError, load_settings

# Local dev convenience: .env next to this file. It never overrides variables
# already set (e.g. by systemd's EnvironmentFile). Only loaded here, so tests,
# which import the package but not this file, never read a developer's .env.
ENV_FILE = Path(__file__).resolve().parent / ".env"

# The exit code for a configuration problem, as for a usage error (a crash exits 1).
CONFIG_ERROR_EXIT = 2
CHECK_CONFIG = "--check-config"
USAGE = f"usage: python bot.py [{CHECK_CONFIG}]"


def start(argv: list[str] | None = None) -> int:
    """Run the bot; on a configuration problem, say what's wrong and return
    CONFIG_ERROR_EXIT instead of starting, with no traceback and no values.
    With --check-config, stop after the check."""
    args = sys.argv[1:] if argv is None else argv
    if args not in ([], [CHECK_CONFIG]):
        print(USAGE, file=sys.stderr)
        return CONFIG_ERROR_EXIT
    _ = load_dotenv(ENV_FILE)
    try:
        _ = load_settings(os.environ)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return CONFIG_ERROR_EXIT
    if args == [CHECK_CONFIG]:
        print("OK: the configuration is valid.")
        return 0
    # Imported only now: importing the app loads the configuration.
    from candygoogy.app import main

    main()
    return 0


if __name__ == "__main__":
    sys.exit(start())
