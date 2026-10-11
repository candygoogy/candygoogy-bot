"""dockerapi.py: the one read-only request to Docker Engine's API, and its answer.
No real socket or connection: httpx.MockTransport answers (conftest refuses the rest)."""

import asyncio
from typing import Self

import httpx
import pytest

from candygoogy import config, dockerapi
from candygoogy.dockerapi import Container
from tests.helpers import container


def answering(
    monkeypatch: pytest.MonkeyPatch, answer: httpx.Response | Exception
) -> list[httpx.Request]:
    """Docker answers `answer` to every request; returns the requests it got."""
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(dockerapi, "transport", lambda: httpx.MockTransport(handle))
    return requests


def answer_id(answer: object) -> str:
    if isinstance(answer, httpx.Response):
        return str(answer.status_code)
    return type(answer).__name__


def test_one_get_of_the_container_list(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = answering(monkeypatch, httpx.Response(200, json=[container("web")]))
    expected = [Container("web", "running", "none", "Up 3 days")]
    assert asyncio.run(dockerapi.list_containers()) == expected
    [request] = requests
    assert request.method == "GET"
    assert request.url.path == "/containers/json"  # no /v1.NN: any Engine version answers
    assert dict(request.url.params) == {"all": "1"}
    assert request.content == b""


@pytest.mark.parametrize(
    ("docker_host", "uds", "base_url"),
    [
        ("unix:///var/run/docker.sock", "/var/run/docker.sock", "http://docker"),
        ("unix:///run/docker-proxy/docker.sock", "/run/docker-proxy/docker.sock", "http://docker"),
        ("tcp://192.0.2.5:2375", None, "http://192.0.2.5:2375"),
        ("tcp://docker-proxy.example:2375", None, "http://docker-proxy.example:2375"),
        ("tcp://[2001:db8::1]:2375", None, "http://[2001:db8::1]:2375"),
    ],
)
def test_where_requests_go(
    monkeypatch: pytest.MonkeyPatch, docker_host: str, uds: str | None, base_url: str
) -> None:
    made: list[dict[str, object]] = []

    def record(**kwargs: object) -> None:
        made.append(kwargs)

    monkeypatch.setattr(dockerapi, "DOCKER_HOST", docker_host)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", record)
    _ = dockerapi.transport()
    assert made == ([{"uds": uds}] if uds else [{}])
    assert dockerapi.base_url() == base_url


def test_the_default_is_dockers_socket() -> None:
    assert config.DOCKER_HOST == "unix:///var/run/docker.sock"


def test_no_proxy_settings_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    clients: list[httpx.AsyncClient] = []

    class Recording(httpx.AsyncClient):
        # typing.override is 3.12+, and the code still supports 3.11.
        async def __aenter__(self) -> Self:  # pyright: ignore[reportImplicitOverride]
            clients.append(self)
            return await super().__aenter__()

    _ = answering(monkeypatch, httpx.Response(200, json=[]))
    monkeypatch.setattr(httpx, "AsyncClient", Recording)
    monkeypatch.setenv("HTTP_PROXY", "http://192.0.2.99:3128")
    monkeypatch.setenv("ALL_PROXY", "http://192.0.2.99:3128")
    assert asyncio.run(dockerapi.list_containers()) == []
    [client] = clients
    assert client.trust_env is False
    assert client.timeout == httpx.Timeout(dockerapi.TIMEOUT_SECONDS)


def test_names_health_and_order(monkeypatch: pytest.MonkeyPatch) -> None:
    listed = [
        container("web", status="Up 2h (unhealthy)"),  # older Engines: only in Status
        container("app", health="healthy", status="Up 3 days (healthy)"),
        container("db", status="Up 1m (health: starting)"),
        container("job", "exited", status="Exited (0) 1h ago"),
        {**container("cron"), "Names": ["/cron", "/other/alias"]},
        {**container("new"), "Health": {"Status": "", "FailingStreak": 0}},
    ]
    _ = answering(monkeypatch, httpx.Response(200, json=listed))
    found = asyncio.run(dockerapi.list_containers())
    assert found == [
        Container("app", "running", "healthy", "Up 3 days (healthy)"),
        Container("cron", "running", "none", "Up 3 days"),
        Container("db", "running", "starting", "Up 1m (health: starting)"),
        Container("job", "exited", "none", "Exited (0) 1h ago"),
        Container("new", "running", "none", "Up 3 days"),
        Container("web", "running", "unhealthy", "Up 2h (unhealthy)"),
    ]


def test_health_field_wins_over_status(monkeypatch: pytest.MonkeyPatch) -> None:
    listed = [container("svc", health="unhealthy", status="Up 2h (healthy)")]
    _ = answering(monkeypatch, httpx.Response(200, json=listed))
    [svc] = asyncio.run(dockerapi.list_containers()) or []
    assert svc.health == "unhealthy"


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("[Errno 2] No such file or directory"),
        httpx.ConnectError("[Errno 13] Permission denied"),
        httpx.ConnectError("[Errno 111] Connection refused"),
    ],
)
def test_nothing_answering_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, error: httpx.ConnectError
) -> None:
    _ = answering(monkeypatch, error)
    with pytest.raises(dockerapi.UnreachableError) as raised:
        _ = asyncio.run(dockerapi.list_containers())
    assert raised.value.__cause__ is error


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(403, text="Forbidden"),
        httpx.Response(404, json={"message": "page not found"}),
        httpx.Response(500, json={"message": "server error"}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"message": "not a list"}),
        httpx.Response(200, json=[["not", "a", "dict"]]),
        httpx.Response(200, json=[{"Names": "/x", "State": "running"}]),
        httpx.Response(200, json=[{"Names": [7], "State": "running"}]),
        httpx.Response(200, json=[{"Names": ["/"], "State": "running"}]),
        httpx.Response(200, json=[{"Names": ["/x"], "State": ""}]),
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("garbled"),
    ],
    ids=answer_id,
)
def test_unusable_answers_are_none(
    monkeypatch: pytest.MonkeyPatch, answer: httpx.Response | Exception
) -> None:
    _ = answering(monkeypatch, answer)
    assert asyncio.run(dockerapi.list_containers()) is None


def test_errors_are_logged_without_the_body(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _ = answering(monkeypatch, httpx.Response(403, text="SECRET-Zq9 forbidden"))
    assert asyncio.run(dockerapi.list_containers()) is None
    assert "Docker's API answered HTTP 403" in caplog.text
    assert "Zq9" not in caplog.text


def test_a_real_transport_is_refused_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guards the guard: without a MockTransport, conftest stops the request."""
    monkeypatch.setattr(dockerapi, "DOCKER_HOST", "tcp://192.0.2.5:2375")
    with pytest.raises(AssertionError, match="real system"):
        _ = asyncio.run(dockerapi.list_containers())
