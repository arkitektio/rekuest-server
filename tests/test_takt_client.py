"""This server's client of takt: what it sends, and how takt's answers and refusals come back.

Asked of the real takt (``takt``). The whole path through it is judged by takt's conformance
suite; this checks the Python side of the wire.
"""

import json

import pytest

from facade import takt, takt_api

pytestmark = pytest.mark.usefixtures("takt")


def test_a_route_answers_with_its_typed_answer() -> None:
    """A call is a POST to the route, and its JSON answer comes back as the route's model."""
    answer = takt.call(takt_api.UPCOMING, takt_api.UpcomingRequest(timings=[takt_api.UpcomingTiming(interval_seconds=60, timezone="UTC", created_at="2026-01-01T00:00:00Z", count=2)]))

    (slots,) = answer.upcoming
    assert slots.error is None and len(slots.slots) == 2 and slots.slots[0] < slots.slots[1]


def test_a_refusal_is_raised_as_takt_worded_it() -> None:
    """takt's ``400`` is a ``ValueError`` carrying its message."""
    with pytest.raises(ValueError, match="Not a valid cron line"):
        takt.call(takt_api.VALIDATE_TIMING, takt_api.Timing(cron="whenever", timezone="UTC"))


def test_an_unreachable_takt_is_said_so(settings: object) -> None:
    """No answer at all is a TaktUnavailable."""
    settings.TAKT_URL = "http://127.0.0.1:9"

    with pytest.raises(takt.TaktUnavailable, match="unreachable"):
        takt.call(takt_api.VALIDATE_TIMING, takt_api.Timing(interval_seconds=60, timezone="UTC"))


def test_without_a_takt_url_it_says_so(settings: object) -> None:
    settings.TAKT_URL = None

    with pytest.raises(takt.TaktUnavailable, match="not configured"):
        takt.call(takt_api.VALIDATE_TIMING, takt_api.Timing(interval_seconds=60, timezone="UTC"))


def test_an_assign_without_nested_overwrites_reads_as_it_always_did() -> None:
    """A mapped agent that pins nothing below sends no ``dependencies`` key: a takt from before
    nested dependencies reads the same payload."""
    from facade.inputs import DependencyTreeInputModel, MappedAgentInputModel, ResolvedDependencyInputModel

    def sent(dependency: ResolvedDependencyInputModel) -> dict:
        """The dependency as it goes out in a request's body."""
        request = takt_api.ResolveRequest(principal=takt_api.Principal(user=1), input=DependencyTreeInputModel(implementation="1", dependencies=[dependency]))
        return json.loads(request.body())["input"]["dependencies"][0]

    flat = ResolvedDependencyInputModel(key="stage", mapped_agents=[MappedAgentInputModel(key="stage", agent="3")])
    assert sent(flat) == {"key": "stage", "mapped_agents": [{"key": "stage", "agent": "3"}], "auto_resolve": False}

    nested = ResolvedDependencyInputModel(key="relay", mapped_agents=[MappedAgentInputModel(key="relay", agent="3", dependencies=[flat])])
    assert sent(nested)["mapped_agents"][0]["dependencies"] == [sent(flat)]
