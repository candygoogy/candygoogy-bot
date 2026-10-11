"""repeat(): the loop behind the bot's periodic checks."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


async def repeat(check: Callable[[], Awaitable[None]], interval: float, what: str) -> None:
    """Run check() every `interval` seconds until cancelled. A failed check is
    logged and doesn't stop the next one."""
    while True:
        try:
            await check()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s failed", what)
        await asyncio.sleep(interval)
