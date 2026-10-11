"""The containers /homelab lists, from Docker Engine's HTTP API.

One read-only request, GET /containers/json?all=1, to DOCKER_HOST: a UNIX
socket (/var/run/docker.sock unless set), or tcp://<host>:<port>, e.g. the
filtering proxy in docker-compose.docker-proxy.yml, which allows only this
request. No docker CLI, and no proxy settings from the environment.

The path carries no API version (/v1.NN/...), so Docker answers in its own
version: every Engine accepts that, old or new, and the fields read here
(Names, State, Status) are the same in all of them.
"""

import logging
from dataclasses import dataclass
from typing import cast

import httpx

from candygoogy.config import DOCKER_HOST

log = logging.getLogger(__name__)

LIST_PATH = "/containers/json"
TIMEOUT_SECONDS = 10.0
UNIX = "unix://"
TCP = "tcp://"
# Recent Engines also send a Health field; older ones say it only in Status,
# e.g. "Up 3 days (unhealthy)".
HEALTH_IN_STATUS = (
    ("(unhealthy)", "unhealthy"),
    ("(healthy)", "healthy"),
    ("(health: starting)", "starting"),
)


@dataclass(frozen=True)
class Container:
    name: str
    state: str  # "running", "exited", "restarting", "paused", "created" or "dead"
    health: str  # "healthy", "unhealthy", "starting" or "none" (no healthcheck)
    status: str  # Docker's summary, e.g. "Exited (1) 5 minutes ago"


class UnreachableError(Exception):
    """Nothing answers at DOCKER_HOST: no socket, no permission, or refused."""


def transport() -> httpx.AsyncBaseTransport:
    """How requests reach Docker. Tests replace it with an httpx.MockTransport."""
    if DOCKER_HOST.startswith(UNIX):
        return httpx.AsyncHTTPTransport(uds=DOCKER_HOST.removeprefix(UNIX))
    return httpx.AsyncHTTPTransport()


def base_url() -> str:
    if DOCKER_HOST.startswith(TCP):
        return "http://" + DOCKER_HOST.removeprefix(TCP)
    return "http://docker"  # over a socket the host name isn't used


def text_field(fields: dict[str, object], name: str) -> str:
    value = fields.get(name)
    return value if isinstance(value, str) else ""


def health_of(fields: dict[str, object], status: str) -> str:
    health = fields.get("Health")
    if isinstance(health, dict):
        found = text_field(cast(dict[str, object], health), "Status")
        if found:
            return found
    return next((value for marker, value in HEALTH_IN_STATUS if marker in status), "none")


def parse_containers(data: object) -> list[Container] | None:
    """The list Docker sent, sorted by name; None if it isn't one."""
    if not isinstance(data, list):
        return None
    found: list[Container] = []
    for entry in cast(list[object], data):
        if not isinstance(entry, dict):
            return None
        fields = cast(dict[str, object], entry)
        names = fields.get("Names")
        first = cast(list[object], names)[0] if isinstance(names, list) and names else None
        name = first.removeprefix("/") if isinstance(first, str) else ""
        state, status = text_field(fields, "State"), text_field(fields, "Status")
        if not name or not state:
            return None
        found.append(Container(name, state, health_of(fields, status), status))
    return sorted(found, key=lambda c: c.name)


async def list_containers() -> list[Container] | None:
    """Every container, running or not; None if Docker's answer wasn't usable.
    Raises UnreachableError if nothing answers at DOCKER_HOST."""
    try:
        async with httpx.AsyncClient(
            transport=transport(), base_url=base_url(), timeout=TIMEOUT_SECONDS, trust_env=False
        ) as client:
            response = await client.get(LIST_PATH, params={"all": "1"})
    except httpx.ConnectError as e:
        raise UnreachableError from e
    except httpx.HTTPError as e:
        log.warning("Docker's API didn't answer (%s)", type(e).__name__)
        return None
    if not response.is_success:
        log.warning("Docker's API answered HTTP %d to the container list", response.status_code)
        return None
    try:
        data = cast(object, response.json())
    except ValueError:
        return None
    return parse_containers(data)
