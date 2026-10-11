"""Small JSON files in STATE_DIR that let the bot's state survive a restart."""

import json
import logging
from pathlib import Path
from typing import cast

log = logging.getLogger(__name__)


def number(value: object) -> float | None:
    """A JSON number, or None (null, missing, a bool, or not a number)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def save_json(path: Path, data: object, what: str) -> bool:
    """Write atomically: a crash leaves the old file or the new one, never half
    of it. False (and a log line) if it failed."""
    tmp = path.with_suffix(".tmp")
    try:
        _ = tmp.write_text(json.dumps(data))
        _ = tmp.replace(path)
    except OSError as e:
        log.warning("Could not save %s to %s: %s", what, path, e)
        return False
    return True


def load_json(path: Path, what: str) -> object | None:
    """The file's contents, or None if there's no file or it can't be read (logged).
    Callers still check the shape, since the file may be from an older version."""
    try:
        return cast(object, json.loads(path.read_text()))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("Ignoring unreadable %s file %s: %s", what, path, e)
        return None
