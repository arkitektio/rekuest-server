"""The reaper as its own process: ``manage.py reaper`` runs the loop, ``--check`` reads its heartbeat.

The loop itself runs for real here (``run_forever``) against the dokker postgres + redis and a
real agent socket — the one test that proves a delayed task is handed over by the loop, not by a
test calling the sweep.
"""

import asyncio
import os
import time
from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.core.management import call_command
from django.utils import timezone

from facade import inputs, messages, reaper
from facade.backend import controll_backend
from facade.caller_context import CallerContext

from tests.agent.helpers import open_agent
from tests.factories import build_implementation_for_agent


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_the_loop_beats_and_hands_over_a_due_task(agent_ws, tmp_path, settings):
    settings.REKUEST_GRACE = {**settings.REKUEST_GRACE, "SWEEP_INTERVAL": 0.1}
    session = await open_agent(agent_ws, "reaper-loop")
    impl = await build_implementation_for_agent(session.agent.pk, "reaper-loop")
    task = await sync_to_async(controll_backend.assign)(
        CallerContext.from_agent(session.agent),
        inputs.AssignInputModel(agent=str(session.agent.pk), interface=impl.interface, args={}, not_before=timezone.now() + timedelta(seconds=0.5)),
    )

    heartbeat = tmp_path / "beat"
    loop = asyncio.ensure_future(reaper.run_forever(heartbeat=heartbeat))
    try:
        assign = await session.receive(messages.Assign, tries=40)
        assert assign.task == str(task.pk)
        assert heartbeat.exists()
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)


class TestCheck:
    def test_fresh_heartbeat_is_healthy(self, tmp_path):
        beat = tmp_path / "beat"
        beat.touch()
        call_command("reaper", "--check", "--heartbeat", str(beat))

    def test_stale_heartbeat_fails(self, tmp_path):
        beat = tmp_path / "beat"
        beat.touch()
        old = time.time() - 600
        os.utime(beat, (old, old))
        with pytest.raises(SystemExit) as exit_info:
            call_command("reaper", "--check", "--heartbeat", str(beat), "--max-age", "60")
        assert exit_info.value.code == 1

    def test_missing_heartbeat_fails(self, tmp_path):
        with pytest.raises(SystemExit) as exit_info:
            call_command("reaper", "--check", "--heartbeat", str(tmp_path / "never"))
        assert exit_info.value.code == 1
