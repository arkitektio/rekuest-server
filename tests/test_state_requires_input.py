"""An assign's state requirements are checked as a port's requires are, and reach takt as sent."""

import json

import pytest
from pydantic import ValidationError

from facade import takt_api
from facade.inputs import AssignInputModel
from facade.inputs.task import StateRequirementInputModel


def test_a_requirement_names_a_state_a_pointer_and_a_fitting_value() -> None:
    requirement = StateRequirementInputModel(state="stage", path="/busy", operator="EQUALS", value=False)
    assert (requirement.state, requirement.path, requirement.value) == ("stage", "/busy", False)
    assert StateRequirementInputModel(state="stage", path="/mode", operator="IN", value=["idle"]).value == ["idle"]
    assert StateRequirementInputModel(state="stage", path="/busy", operator="EXISTS").value is None


@pytest.mark.parametrize(
    ("sent", "message"),
    [
        ({"path": "busy", "operator": "EQUALS", "value": 1}, "pattern"),
        ({"path": "/mode", "operator": "IN", "value": "idle"}, "IN needs a list value"),
        ({"path": "/x", "operator": "GTE", "value": True}, "GTE needs a numeric value"),
        ({"path": "/x", "operator": "EXISTS", "value": 1}, "EXISTS takes no value or a boolean"),
        ({"path": "/x", "operator": "EQUALS", "value": 1, "key": "x"}, "Extra inputs"),
    ],
)
def test_a_requirement_that_could_never_be_tested_is_refused(sent: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        StateRequirementInputModel(state="stage", **sent)


def test_takt_is_sent_the_requirements_and_nothing_when_there_are_none() -> None:
    principal = takt_api.Principal(user=1, client=1, organization=1)
    plain = json.loads(takt_api.AssignRequest(principal=principal, input=AssignInputModel(implementation="1", args={})).body())
    assert "state_requires" not in plain["input"]

    wanted = AssignInputModel(
        implementation="1",
        args={},
        state_requires=[StateRequirementInputModel(state="stage", path="/busy", operator="EQUALS", value=False)],
    )
    sent = json.loads(takt_api.AssignRequest(principal=principal, input=wanted).body())
    assert sent["input"]["state_requires"] == [{"state": "stage", "path": "/busy", "operator": "EQUALS", "value": False}]
