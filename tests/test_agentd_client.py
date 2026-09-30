"""The control backend's client of agentd: what it sends, and how agentd's refusals come back.

agentd itself (and the whole path through it) is judged by rekuest-agentd's conformance suite;
this checks only the Python side of the wire.
"""

import json
from collections.abc import Callable

import httpx
import pytest

from facade import agentd


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch, settings: object) -> Callable[..., list[httpx.Request]]:
    """agentd answered by ``handler``; every request it saw."""
    settings.AGENTD_URL = "http://agentd:8080/rekuest"
    seen: list[httpx.Request] = []

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
        def record(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        monkeypatch.setattr(agentd, "_client", httpx.Client(transport=httpx.MockTransport(record)))
        return seen

    return install


def test_a_call_is_a_signed_post_to_the_internal_route(served: Callable[..., list[httpx.Request]]) -> None:
    """The request agentd receives."""
    seen = served(lambda request: httpx.Response(200, json={"task": "7"}))

    assert agentd.call("cancel", {"task": "7"}) == {"task": "7"}

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "http://agentd:8080/rekuest/internal/cancel"
    assert json.loads(request.content) == {"task": "7"}
    assert request.headers["Authorization"].startswith("RekuestService ")


@pytest.mark.parametrize(
    ("status", "raised"),
    [(400, ValueError), (403, PermissionError), (401, agentd.AgentdUnavailable), (409, agentd.AgentdUnavailable)],
)
def test_refusals_come_back_as_the_in_process_backend_raised_them(served: Callable[..., list[httpx.Request]], status: int, raised: type[Exception]) -> None:
    """agentd's refusal statuses map to the in-process backend's exceptions."""
    served(lambda request: httpx.Response(status, json={"error": "Task 7 is already done"}))

    with pytest.raises(raised, match="Task 7 is already done"):
        agentd.call("cancel", {"task": "7"})


def test_an_unreachable_agentd_is_said_so(served: Callable[..., list[httpx.Request]]) -> None:
    """No answer at all is an AgentdUnavailable."""

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    served(down)

    with pytest.raises(agentd.AgentdUnavailable, match="unreachable"):
        agentd.call("assign", {})
