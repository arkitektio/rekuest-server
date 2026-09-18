"""The ``taskStats`` / ``actionStats`` queries over their timestamp field.

Postgres has no ``avg()``/``sum()`` over timestamps, so the stats type aggregates temporal
fields as epoch seconds (and leaves their meaningless ``sum`` null).
"""

import pytest

from facade.schema import schema
from tests.factories import build_task_for_agent_caller, seed_agent

STATS = """
    query {
        %s {
            count
            distinctCount(field: CREATED_AT)
            max(field: CREATED_AT)
            min(field: CREATED_AT)
            avg(field: CREATED_AT)
            sum(field: CREATED_AT)
            series(field: CREATED_AT, timestampField: CREATED_AT, by: DAY) {
                ts
                count
                max
                min
                avg
                sum
            }
        }
    }
"""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestStats:
    async def test_task_stats_aggregate_timestamps_as_epoch(self, authenticated_context):
        agent = await seed_agent("stats", token="test")
        first = await build_task_for_agent_caller(agent.pk, "stats-first")
        second = await build_task_for_agent_caller(agent.pk, "stats-second")

        result = await schema.execute(STATS % "taskStats", context_value=authenticated_context)

        assert not result.errors, result.errors
        stats = result.data["taskStats"]
        assert stats["count"] >= 2
        assert stats["min"] <= first.created_at.timestamp() <= second.created_at.timestamp() <= stats["max"]
        assert stats["min"] <= stats["avg"] <= stats["max"]
        assert stats["sum"] is None
        assert sum(bucket["count"] for bucket in stats["series"]) == stats["count"]
        assert all(bucket["sum"] is None for bucket in stats["series"])

    async def test_action_stats_resolve(self, authenticated_context):
        # Action has no created_at column; CREATED_AT maps onto defined_at.
        await seed_agent("stats-action", token="test")

        result = await schema.execute(STATS % "actionStats", context_value=authenticated_context)

        assert not result.errors, result.errors
        assert result.data["actionStats"]["sum"] is None
