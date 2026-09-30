"""Fields that were served without a column behind them resolve now.

``Session.startedAt``/``endedAt``, ``Shortcut.toolboxes`` and ``TestResult.updatedAt`` were in the
schema but named nothing on their models, so any query selecting them failed. ``startedAt`` reads
the session's creation time (``endedAt`` had no data at all and is gone), a shortcut has exactly
one ``toolbox``, and a test result is never updated.
"""

import pytest
from asgiref.sync import sync_to_async
from authentikate.models import App, Release
from kante.context import HttpContext

from facade import models
from facade.schema import schema


def _rows(context: HttpContext) -> None:
    request = context.request
    client = request.client
    if not Release.objects.filter(pk=client.release_id).exists():
        client.release = Release.objects.create(app=App.objects.get_or_create(identifier="backed-fields")[0], version="1")
        client.save()
    agent, _ = models.Agent.objects.get_or_create(
        client=client, user=request.user, organization=request.organization, defaults={"name": "backed", "app": client.release.app, "release": client.release}
    )
    models.Session.objects.create(agent=agent, session_id="first")
    models.Session.objects.create(agent=agent, session_id="second")
    toolbox = models.Toolbox.objects.create(name="box", description="", creator=request.user, client=client, organization=request.organization)
    models.Shortcut.objects.create(name="quick", toolbox=toolbox, creator=request.user)


QUERY = """
    query {
        sessions(ordering: [{startedAt: DESC}]) { startedAt }
        shortcuts { name toolbox { name } }
    }
"""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_session_start_and_shortcut_toolbox_resolve(authenticated_context: HttpContext) -> None:
    """A session's start and a shortcut's toolbox resolve, and sessions order by their start."""
    await sync_to_async(_rows)(authenticated_context)

    result = await schema.execute(QUERY, context_value=authenticated_context)

    assert result.errors is None, result.errors
    starts = [s["startedAt"] for s in result.data["sessions"]]
    assert len(starts) == 2 and starts == sorted(starts, reverse=True)
    assert result.data["shortcuts"] == [{"name": "quick", "toolbox": {"name": "box"}}]
