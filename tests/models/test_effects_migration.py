"""Migration 0016 carries an implementation's old effect class over to ``effects``.

PHYSICAL said what IRREVERSIBLE says, and has to survive the rename. NONE was only ever
the default, so it claimed nothing and becomes UNKNOWN. On every test database the table
is empty, so this is only exercised here: the test migrates back to 0015, seeds rows the
way 0015 stores them, and migrates forward again.
"""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from facade.models import Action
from tests.factories import _seed_throwaway_agent_graph

pytestmark = pytest.mark.django_db(transaction=True)

BEFORE = [("facade", "0015_agent_positions")]
AFTER = [("facade", "0016_implementation_effects_execution")]


def _migrate(target: list[tuple[str, str]]):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(target)
    return executor.loader.project_state(target).apps


def test_physical_becomes_irreversible_and_none_claims_nothing() -> None:
    old = _migrate(BEFORE)
    try:
        # Agents and actions look the same before and after 0016; only the implementation's
        # columns change, so only it is written through the historical model.
        agent = _seed_throwaway_agent_graph("mig16")
        robot, count = (
            Action.objects.create(
                app=agent.app,
                key=f"mig16-{key}",
                version="1.0.0",
                name=f"mig16 {key}",
                description="mig16",
                hash=f"mig16-{key}-hash",
                organization=agent.organization,
            )
            for key in ("robot", "count")
        )
        Implementation = old.get_model("facade", "Implementation")
        physical = Implementation.objects.create(interface="robot", agent_id=agent.pk, action_id=robot.pk, release_id=agent.release_id, effect="PHYSICAL")
        plain = Implementation.objects.create(interface="count", agent_id=agent.pk, action_id=count.pk, release_id=agent.release_id, effect="NONE")
    finally:
        new = _migrate(AFTER)

    Implementation = new.get_model("facade", "Implementation")
    assert Implementation.objects.get(pk=physical.pk).effects == "IRREVERSIBLE"
    assert Implementation.objects.get(pk=plain.pk).effects == "UNKNOWN"
    assert Implementation.objects.get(pk=plain.pk).execution == "PLAIN"
