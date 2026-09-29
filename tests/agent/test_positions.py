"""Numbered agent frames on the server: one projection per session position, cumulative acks,
task steps and effects, the agent's outcome after a restart, agent-minted shelving.

The contract is ``docs/design/journal.md``. A numbering agent stamps ``pos`` / ``journal_session``
/ ``agent_ts`` (and ``task_step`` on frames of a task) and re-sends whatever a ``JOURNAL_ACK`` has
not covered. The server keeps one watermark per session (``Session.projected_pos``); a frame at or
below it is a resend. ``tests/fixtures/agent_wire.json`` holds the canonical frames every agent
and this server must parse alike.
"""

import asyncio
import json
import pathlib
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async
from django.db import connections
from django.utils import timezone
from pydantic import BaseModel, Field

from facade import codes, enums, inputs, messages, models
from facade.consumers.agent_protocol import FromAgentPayload
from facade.message_router import route_from_agent_message
from facade.models import MemoryDrawer, Patch, Session, TaskEvent
from facade.persist.positions import CLAIM_TIMEOUT_SECONDS
from facade.persist_backend import ModelPersistBackend

from tests.agent.helpers import AgentSession, open_agent
from tests.factories import TEST_TOKEN, _build_task, _seed_throwaway_agent_graph, build_implementation_for_agent, build_state_for_agent, build_task

JOURNAL_ACK = messages.ToAgentMessageType.JOURNAL_ACK.value
EVENT_ACK = messages.ToAgentMessageType.EVENT_ACK.value
SHELVED = messages.ToAgentMessageType.SHELVED.value
UNSHELVED = messages.ToAgentMessageType.UNSHELVED.value
FIXTURES = pathlib.Path(__file__).parent.parent / "fixtures" / "agent_wire.json"


def _j(pos: int, session: str = "proc-1", step: int | None = None) -> dict:
    """The position fields of one frame."""
    fields: dict = {"pos": pos, "journal_session": session, "agent_ts": time.time()}
    if step is not None:
        fields["task_step"] = step
    return fields


async def _frames(session: AgentSession, seconds: float) -> list[dict]:
    """Every frame the server sends within ``seconds`` (drained, in order).

    Polls with ``receive_nothing``: a ``receive_*`` that times out cancels the application.
    """
    frames = []
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if not await session.communicator.receive_nothing(timeout=0.05):
            frames.append(await session.communicator.receive_json_from())
    return frames


async def _ack_until(session: AgentSession, pos: int) -> list[int]:
    """Read JOURNAL_ACKs until one covers ``pos``; return every acked position seen, in order."""
    seen: list[int] = []
    for _ in range(40):
        ack = await session.receive(messages.JournalAck)
        seen.append(ack.pos)
        if ack.pos >= pos:
            return seen
    raise AssertionError(f"no JOURNAL_ACK reached {pos}: {seen}")


def _types(frames: list[dict]) -> set:
    return {f.get("type") for f in frames}


async def _projected(agent_pk: int, session_id: str = "proc-1") -> int:
    return await Session.objects.filter(agent_id=agent_pk, session_id=session_id).values_list("projected_pos", flat=True).aget()


class ToAgentPayload(BaseModel):
    message: messages.ToAgentMessage = Field(discriminator="type")


class TestWireFixtures:
    """The frames in ``agent_wire.json`` are what the Rust and Python agents send and parse too."""

    def _fixtures(self) -> dict:
        return json.loads(FIXTURES.read_text())

    @pytest.mark.parametrize("group", ["numbered_from_agent", "unnumbered_from_agent"])
    def test_every_agent_frame_parses_and_round_trips(self, group):
        for case in self._fixtures()[group]:
            frame = case["frame"]
            parsed = FromAgentPayload(message=frame).message
            dumped = parsed.model_dump(mode="json", exclude_none=True, by_alias=True)
            assert {k: v for k, v in dumped.items() if k in frame} == frame, case["name"]
            assert set(frame) <= set(dumped), case["name"]

    def test_every_numbered_frame_carries_its_position_and_task_frames_their_step(self):
        from facade.persist.positions import is_numbered

        for case in self._fixtures()["numbered_from_agent"]:
            parsed = FromAgentPayload(message=case["frame"]).message
            assert is_numbered(parsed), case["name"]
            has_task = getattr(parsed, "task", None) or getattr(parsed, "task_id", None)
            assert (parsed.task_step is not None) == bool(has_task), case["name"]
        for case in self._fixtures()["unnumbered_from_agent"]:
            assert not is_numbered(FromAgentPayload(message=case["frame"]).message), case["name"]

    def test_every_server_frame_parses_and_round_trips(self):
        for case in self._fixtures()["to_agent"]:
            frame = case["frame"]
            dumped = ToAgentPayload(message=frame).message.model_dump(mode="json", exclude_none=True)
            assert {k: v for k, v in dumped.items() if k in frame} == frame, case["name"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestPositions:
    async def test_resent_frames_are_projected_once_and_not_event_acked(self, agent_ws):
        session = await open_agent(agent_ws, "pos-resend", session_id="proc-1")
        task = await build_task("pos-resend", agent_pk=session.agent_pk)

        started = messages.Started(task=str(task.pk), **_j(1, step=1))
        log = messages.Log(task=str(task.pk), message="hello", **_j(2, step=2))
        for message in (started, log, started, log):  # the second pair is the resend
            await session.send(message)
        await _ack_until(session, 2)
        # JOURNAL_ACK covers numbered frames: no EventAck on top.
        assert EVENT_ACK not in _types(await _frames(session, 0.4))
        await session.disconnect()

        assert await TaskEvent.objects.filter(task_id=task.pk, kind=enums.TaskEventKind.STARTED).acount() == 1
        event = await TaskEvent.objects.filter(task_id=task.pk, kind=enums.TaskEventKind.LOG).aget()
        assert (event.agent_pos, event.step) == (2, 2)
        assert event.agent_ts is not None
        assert await _projected(session.agent_pk) == 2

    async def test_resend_on_a_new_connection_is_skipped_and_acked(self, agent_ws, settings):
        # A reconnect within the grace window: the task was never lost, so the same process
        # carries on with it. (Past the grace window it would be LOST, and LOST is final.)
        settings.REKUEST_GRACE = {**settings.REKUEST_GRACE, "DEFAULT": 30}
        session = await open_agent(agent_ws, "pos-reconnect", session_id="proc-1")
        task = await build_task("pos-reconnect", agent_pk=session.agent_pk)
        for pos in (1, 2, 3):
            await session.send(messages.Progress(task=str(task.pk), progress=pos, **_j(pos, step=pos)))
        await _ack_until(session, 3)
        await session.disconnect()

        again = AgentSession(await agent_ws(), agent=session.agent)
        await again.register(token=TEST_TOKEN, force=True, session_id="proc-1")
        for pos in (2, 3, 4):  # 2 and 3 are resends, 4 is new
            await again.send(messages.Progress(task=str(task.pk), progress=pos, **_j(pos, step=pos)))
        assert (await _ack_until(again, 4))[-1] == 4
        await again.disconnect()

        assert await TaskEvent.objects.filter(task_id=task.pk, kind=enums.TaskEventKind.PROGRESS).acount() == 4

    async def test_a_gap_the_agent_cannot_fill_is_skipped(self, agent_ws):
        """The agent sends from its lowest unacked position, in order: a frame beyond the
        watermark + 1 means the ones between are gone. Waiting for them would hold the ack back
        forever."""
        session = await open_agent(agent_ws, "pos-gap", session_id="proc-1")
        task = await build_task("pos-gap", agent_pk=session.agent_pk)
        await session.send(messages.Log(task=str(task.pk), message="1", **_j(1, step=1)))
        await session.send(messages.Log(task=str(task.pk), message="5", **_j(5, step=5)))
        assert (await _ack_until(session, 5))[-1] == 5
        await session.disconnect()
        assert [e.message async for e in TaskEvent.objects.filter(task_id=task.pk, kind=enums.TaskEventKind.LOG).order_by("id")] == ["1", "5"]

    async def test_acks_are_cumulative_and_a_terminal_is_acked_at_once(self, agent_ws):
        session = await open_agent(agent_ws, "pos-cumulative", session_id="proc-1")
        task = await build_task("pos-cumulative", agent_pk=session.agent_pk)
        for pos in range(1, 6):
            await session.send(messages.Log(task=str(task.pk), message=f"line {pos}", **_j(pos, step=pos)))
        await session.send(messages.Completed(task=str(task.pk), **_j(6, step=6)))

        seen = await _ack_until(session, 6)
        assert seen == sorted(seen), "acks never go backwards"
        assert len(seen) < 6, "acks are cumulative, not per frame"
        await session.disconnect()

    async def test_a_lone_frame_is_acked_after_the_debounce(self, agent_ws):
        session = await open_agent(agent_ws, "pos-debounce", session_id="proc-1")
        task = await build_task("pos-debounce", agent_pk=session.agent_pk)
        await session.send(messages.Log(task=str(task.pk), message="only", **_j(1, step=1)))
        acks = [f for f in await _frames(session, 1.0) if f.get("type") == JOURNAL_ACK]
        assert acks == [{"type": JOURNAL_ACK, "id": acks[0]["id"], "journal_session": "proc-1", "pos": 1}]
        await session.disconnect()

    async def test_an_agent_without_numbering_gets_event_acks_and_no_journal_ack(self, agent_ws):
        session = await open_agent(agent_ws, "pos-old")
        task = await build_task("pos-old", agent_pk=session.agent_pk)
        await session.send(messages.Started(task=str(task.pk), seq=1))
        await session.send(messages.Log(task=str(task.pk), message="hi", seq=2))
        await session.send(messages.Completed(task=str(task.pk), seq=3))

        frames = await _frames(session, 0.8)  # well past the ack debounce
        assert JOURNAL_ACK not in _types(frames)
        assert [a["seq"] for a in frames if a.get("type") == EVENT_ACK] == [1, 3]
        await session.disconnect()
        assert [e.agent_pos async for e in TaskEvent.objects.filter(task_id=task.pk)] == [None] * await TaskEvent.objects.filter(task_id=task.pk).acount()

    async def test_resent_patch_is_applied_once_with_its_step(self, agent_ws):
        session = await open_agent(agent_ws, "pos-patch", session_id="proc-1")
        await build_state_for_agent(session.agent_pk, interface="counter", prefix="pos-patch")
        task = await build_task("pos-patch", agent_pk=session.agent_pk)

        init = messages.SessionInit(session_id="proc-1", states={"counter": {"value": 0}}, **_j(1))
        patch = messages.StatePatch(
            session_id="proc-1", global_rev=1, state_name="counter", ts=0.0, op="replace", path="/value", value=5, old_value=0, task_id=str(task.pk), **_j(2, step=3)
        )
        for message in (init, patch, init, patch):
            await session.send(message)
        await _ack_until(session, 2)
        await session.disconnect()

        stored = await Patch.objects.filter(agent_id=session.agent_pk).aget()
        assert (stored.old_value, stored.agent_pos, stored.step) == (0, 2, 3)
        assert await models.Snapshot.objects.filter(agent_id=session.agent_pk).acount() == 1

    async def test_a_refused_frame_does_not_block_the_ack(self, agent_ws):
        """A patch for a state the agent never declared raises on every delivery; retained until
        acked, it would be re-sent forever. It counts as handled instead."""
        session = await open_agent(agent_ws, "pos-poison", session_id="proc-1")
        await session.send(messages.StatePatch(session_id="proc-1", global_rev=1, state_name="nope", ts=0.0, op="add", path="/x", value=1, old_value=None, **_j(1)))
        await session.send(messages.Unlock(key="some-lock", **_j(2)))
        assert (await _ack_until(session, 2))[-1] == 2
        await session.disconnect()

    async def test_a_probes_state_patch_is_numbered_and_kept_without_a_task_link(self, agent_ws):
        """The global_rev chain must not have holes: a patch a probe caused is numbered like
        any other, and stored without linking the (row-less) probe."""
        session = await open_agent(agent_ws, "pos-probe-patch", session_id="proc-1")
        await build_state_for_agent(session.agent_pk, interface="counter", prefix="pos-probe-patch")
        await session.send(messages.SessionInit(session_id="proc-1", states={"counter": {"value": 0}}, **_j(1)))
        await session.send(
            messages.StatePatch(session_id="proc-1", global_rev=1, state_name="counter", ts=0.0, op="replace", path="/value", value=1, old_value=0, task_id="p-" + "1" * 32, **_j(2))
        )
        assert (await _ack_until(session, 2))[-1] == 2
        await session.disconnect()
        stored = await Patch.objects.filter(agent_id=session.agent_pk).aget()
        assert (stored.global_rev, stored.task_id, stored.agent_pos) == (1, None, 2)

    async def test_probe_frames_are_never_numbered(self, agent_ws):
        session = await open_agent(agent_ws, "pos-probe", session_id="proc-1")
        await session.send(messages.Log(task="p-" + "0" * 32, message="probing", **_j(1, session="probe-session", step=1)))
        assert JOURNAL_ACK not in _types(await _frames(session, 0.6))
        await session.disconnect()
        assert not await Session.objects.filter(agent_id=session.agent_pk, session_id="probe-session").aexists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestTaskHistory:
    async def test_an_effect_is_a_task_event_at_its_step(self, agent_ws):
        session = await open_agent(agent_ws, "pos-effect", session_id="proc-1")
        task = await build_task("pos-effect", agent_pk=session.agent_pk)
        t = str(task.pk)
        await session.send(messages.Effect(task=t, effect="NOW", value=1790000000.5, **_j(1, step=1)))
        await session.send(messages.Effect(task=t, effect="RANDOM", value="deadbeef", **_j(2, step=2)))
        await session.send(messages.Effect(task=t, effect="SLEEP", value=1790000010.0, **_j(3, step=3)))
        await _ack_until(session, 3)
        assert _types(await _frames(session, 0.3)) <= {JOURNAL_ACK}, "effects are never answered"
        await session.disconnect()

        effects = [(e.step, e.effect, e.value) async for e in TaskEvent.objects.filter(task_id=task.pk, kind=enums.TaskEventKind.EFFECT).order_by("step")]
        assert effects == [(1, "NOW", 1790000000.5), (2, "RANDOM", "deadbeef"), (3, "SLEEP", 1790000010.0)]

    async def test_nothing_is_recorded_on_a_done_task(self, agent_ws):
        session = await open_agent(agent_ws, "pos-after-end", session_id="proc-1")
        task = await build_task("pos-after-end", agent_pk=session.agent_pk)
        t = str(task.pk)
        await session.send(messages.Completed(task=t, **_j(1, step=1)))
        await session.send(messages.Yield(task=t, returns={"late": 1}, **_j(2, step=2)))
        await session.send(messages.Effect(task=t, effect="NOW", value=1.0, **_j(3, step=3)))
        assert (await _ack_until(session, 3))[-1] == 3
        await session.disconnect()

        kinds = {e.kind async for e in TaskEvent.objects.filter(task_id=task.pk)}
        assert enums.TaskEventKind.COMPLETED in kinds
        assert not kinds & {enums.TaskEventKind.YIELD, enums.TaskEventKind.EFFECT}

    async def test_a_child_call_is_found_again_by_its_parent_step(self, agent_ws):
        """No CALL frame: the child task with ``parent_step`` is the record. A call re-issued
        after a restart carries a fresh reference but the same step, and gets the same child;
        the caller's reference is kept as it sent it."""
        session = await open_agent(agent_ws, "pos-call", session_id="proc-1")
        impl = await build_implementation_for_agent(session.agent.pk, "pos-call")
        parent = await build_task("pos-call-parent")

        await session.send(messages.AssignRequest(reference="first-ref", parent_step=3, implementation=str(impl.pk), parent=str(parent.pk), args={"x": 1}))
        first = await session.receive(messages.AssignResponse)
        assert first.created is True and first.task and first.reference == "first-ref"
        await session.disconnect()

        restarted = AgentSession(await agent_ws(), agent=session.agent)
        await restarted.register(token=TEST_TOKEN, force=True, session_id="proc-2")
        await restarted.send(messages.AssignRequest(reference="fresh-ref", parent_step=3, implementation=str(impl.pk), parent=str(parent.pk), args={"x": 1}))
        second = await restarted.receive(messages.AssignResponse)
        assert second.task == first.task and second.created is False and second.reference == "first-ref"

        # Another step of the same parent is another child; without a reference the server mints one.
        await restarted.send(messages.AssignRequest(parent_step=4, implementation=str(impl.pk), parent=str(parent.pk), args={"x": 2}))
        third = await restarted.receive(messages.AssignResponse)
        assert third.created is True and third.task != first.task and third.reference
        await restarted.disconnect()

        child = await models.Task.objects.aget(pk=first.task)
        assert (child.parent_id, child.parent_step, child.reference) == (parent.pk, 3, "first-ref")


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestOutcomeAfterRestart:
    """After a restart the agent resends its previous session's unacked frames — by then the new
    session's registration has already orphaned that session's in-flight work."""

    async def _opened(self, agent_ws, prefix: str):
        session = await open_agent(agent_ws, prefix, session_id="proc-1")
        task = await build_task(prefix, agent_pk=session.agent_pk)
        return session, task

    async def _restart(self, agent_ws, session):
        await session.disconnect()
        restarted = AgentSession(await agent_ws(), agent=session.agent)
        await restarted.register(token=TEST_TOKEN, force=True, session_id="proc-2")
        return restarted

    async def test_a_restart_leaves_in_flight_work_lost_and_a_late_outcome_is_kept_beside_it(self, agent_ws):
        """LOST is final: whoever called may already have acted on it. The previous session's
        outcome, resent after the restart, is kept as a LATE_REPORT and changes nothing."""
        session, task = await self._opened(agent_ws, "pos-lost")
        restarted = await self._restart(agent_ws, session)
        assert (await models.Task.objects.aget(pk=task.pk)).latest_event_kind == enums.TaskEventKind.LOST

        await restarted.send(messages.Completed(task=str(task.pk), **_j(9, session="proc-1", step=4)))
        await restarted.send(messages.Completed(task=str(task.pk), **_j(9, session="proc-1", step=4)))  # a resend
        await _ack_until(restarted, 9)
        await restarted.disconnect()

        task = await models.Task.objects.aget(pk=task.pk)
        assert task.is_done and task.latest_event_kind == enums.TaskEventKind.LOST
        late = [e async for e in TaskEvent.objects.filter(task_id=task.pk, kind=enums.TaskEventKind.LATE_REPORT)]
        assert len(late) == 1, "a resend of the same late report is recorded once"
        assert late[0].value == {"kind": "COMPLETED"} and late[0].agent_pos == 9

    async def test_the_agents_outcome_replaces_a_server_outcome_that_is_not_lost(self, agent_ws):
        session, task = await self._opened(agent_ws, "pos-wins")
        await ModelPersistBackend()._finalize_terminal(task.pk, enums.TaskEventKind.CRITICAL, "decided by the server")
        restarted = await self._restart(agent_ws, session)

        await restarted.send(messages.Completed(task=str(task.pk), **_j(9, session="proc-1", step=4)))
        await _ack_until(restarted, 9)
        await restarted.disconnect()

        task = await models.Task.objects.aget(pk=task.pk)
        assert task.is_done and task.latest_event_kind == enums.TaskEventKind.COMPLETED
        completed = await TaskEvent.objects.aget(task_id=task.pk, kind=enums.TaskEventKind.COMPLETED)
        assert completed.agent_pos == 9 and "previous session" in completed.message

    async def test_an_outcome_the_agent_reported_stands(self, agent_ws):
        session, task = await self._opened(agent_ws, "pos-stands")
        await session.send(messages.Failed(task=str(task.pk), error="real failure", **_j(1, session="proc-1", step=1)))
        await _ack_until(session, 1)
        restarted = await self._restart(agent_ws, session)

        await restarted.send(messages.Completed(task=str(task.pk), **_j(9, session="proc-1", step=4)))
        await _ack_until(restarted, 9)
        await restarted.disconnect()
        assert (await models.Task.objects.aget(pk=task.pk)).latest_event_kind == enums.TaskEventKind.FAILED

    async def test_the_current_session_cannot_replace_the_servers_outcome(self, agent_ws):
        session, task = await self._opened(agent_ws, "pos-current")
        await ModelPersistBackend()._finalize_terminal(task.pk, enums.TaskEventKind.CANCELLED, "cancelled by the server")
        restarted = await self._restart(agent_ws, session)

        await restarted.send(messages.Completed(task=str(task.pk), **_j(1, session="proc-2", step=1)))
        await _ack_until(restarted, 1)
        await restarted.disconnect()
        assert (await models.Task.objects.aget(pk=task.pk)).latest_event_kind == enums.TaskEventKind.CANCELLED



def _log(task, pos: int, session: str = "race-1") -> messages.Log:
    return messages.Log(task=str(task.pk), message=f"at {pos}", pos=pos, journal_session=session, agent_ts=time.time(), task_step=pos)


def _deliver_in_thread(agent_pk: int, message) -> threading.Thread:
    """Deliver ``message`` as a second backend would: its own thread, loop and DB connection."""

    def run():
        try:
            asyncio.run(route_from_agent_message(ModelPersistBackend(), agent_pk, message))
        finally:
            connections.close_all()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


@pytest.mark.django_db(transaction=True)
class TestTwoBackends:
    """The same frame reaching two backends (the agent reconnected elsewhere while the first was
    still working). Real threads, and the first backend is held by its claim, not by sleeping."""

    def test_the_second_backend_waits_for_the_first_and_does_not_project_again(self):
        agent = _seed_throwaway_agent_graph("pos-race")
        task = _build_task("pos-race-task", agent_pk=agent.pk)
        # Backend A has claimed position 1 and is projecting it.
        session = Session.objects.create(agent=agent, session_id="race-1", projected_pos=0, claimed_pos=1, claimed_at=timezone.now())

        b = _deliver_in_thread(agent.pk, _log(task, 1))
        b.join(0.5)
        assert b.is_alive(), "B must wait while A's claim is live"

        # A finishes: its projection, then its confirm.
        TaskEvent.objects.create(task=task, kind=enums.TaskEventKind.LOG, message="at 1", agent_pos=1, step=1)
        Session.objects.filter(pk=session.pk).update(projected_pos=1)
        b.join(5)
        assert not b.is_alive()
        assert TaskEvent.objects.filter(task=task, kind=enums.TaskEventKind.LOG).count() == 1

    def test_a_stale_claim_is_taken_over(self):
        """A's backend died mid-projection: after the claim timeout the frame is projected by
        whoever has it next."""
        agent = _seed_throwaway_agent_graph("pos-stale")
        task = _build_task("pos-stale-task", agent_pk=agent.pk)
        stale = timezone.now() - timedelta(seconds=CLAIM_TIMEOUT_SECONDS + 5)
        Session.objects.create(agent=agent, session_id="race-1", projected_pos=0, claimed_pos=1, claimed_at=stale)

        b = _deliver_in_thread(agent.pk, _log(task, 1))
        b.join(5)
        assert not b.is_alive()
        assert TaskEvent.objects.filter(task=task, kind=enums.TaskEventKind.LOG).count() == 1
        assert Session.objects.get(agent=agent, session_id="race-1").projected_pos == 1

    def test_concurrent_deliveries_project_once(self):
        agent = _seed_throwaway_agent_graph("pos-both")
        task = _build_task("pos-both-task", agent_pk=agent.pk)
        threads = [_deliver_in_thread(agent.pk, _log(task, 1)) for _ in range(4)]
        for thread in threads:
            thread.join(10)
        assert not any(t.is_alive() for t in threads)
        assert TaskEvent.objects.filter(task=task, kind=enums.TaskEventKind.LOG).count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestAgentMintedShelving:
    async def test_a_numbered_shelve_creates_an_agent_minted_drawer_without_a_reply(self, agent_ws):
        session = await open_agent(agent_ws, "pos-shelve", session_id="proc-1")
        await session.send(messages.Shelve(ref="minted-1", identifier="@test/thing", resource_id="minted-1", label="a thing", **_j(1)))
        assert (await _ack_until(session, 1))[-1] == 1
        assert SHELVED not in _types(await _frames(session, 0.5))

        drawer = await MemoryDrawer.objects.select_related("shelve").aget(resource_id="minted-1")
        assert drawer.agent_minted is True and drawer.shelve.agent_id == session.agent_pk
        assert drawer.identifier == "@test/thing" and drawer.label == "a thing"

        await session.send(messages.Shelve(ref="minted-1", identifier="@test/thing", resource_id="minted-1", **_j(1)))
        await _ack_until(session, 1)
        assert await MemoryDrawer.objects.filter(shelve__agent_id=session.agent_pk).acount() == 1
        await session.disconnect()

    async def test_a_numbered_unshelve_finds_the_drawer_by_resource_id(self, agent_ws):
        session = await open_agent(agent_ws, "pos-unshelve", session_id="proc-1")
        await session.send(messages.Shelve(ref="minted-2", identifier="@test/thing", resource_id="minted-2", **_j(1)))
        await session.send(messages.Unshelve(drawer="minted-2", **_j(2)))
        assert (await _ack_until(session, 2))[-1] == 2
        assert UNSHELVED not in _types(await _frames(session, 0.5))
        assert not await MemoryDrawer.objects.filter(resource_id="minted-2").aexists()

        legacy = await sync_to_async(_legacy_drawer)(session.agent_pk, "legacy-1")
        await session.send(messages.Unshelve(drawer=str(legacy.pk), **_j(3)))
        await session.send(messages.Unshelve(drawer="never-shelved", **_j(4)))  # refused, handled
        assert (await _ack_until(session, 4))[-1] == 4
        assert not await MemoryDrawer.objects.filter(pk=legacy.pk).aexists()
        await session.disconnect()

    async def test_a_shelve_of_an_earlier_session_is_not_upserted(self, agent_ws):
        session = await open_agent(agent_ws, "pos-stale-shelve", session_id="proc-2")
        await session.send(messages.Shelve(ref="old-1", identifier="@test/thing", resource_id="old-1", **_j(7, session="proc-1")))
        assert (await _ack_until(session, 7))[-1] == 7
        await session.disconnect()
        assert not await MemoryDrawer.objects.filter(resource_id="old-1").aexists()

    async def test_an_unnumbered_agents_shelve_and_unshelve_are_unchanged(self, agent_ws):
        session = await open_agent(agent_ws, "pos-old-shelve")
        await session.send(messages.Shelve(ref="r1", identifier="@test/thing", resource_id="old-agent-1"))
        shelved = await session.receive(messages.Shelved)
        assert shelved.ref == "r1" and shelved.error is None
        drawer = await MemoryDrawer.objects.aget(pk=shelved.drawer)
        assert drawer.agent_minted is False and drawer.resource_id == "old-agent-1"

        await session.send(messages.Unshelve(ref="r2", drawer="old-agent-1"))
        assert "Unknown drawer" in ((await session.receive(messages.Unshelved)).error or "")
        await session.send(messages.Unshelve(ref="r3", drawer=shelved.drawer))
        unshelved = await session.receive(messages.Unshelved)
        assert unshelved.ref == "r3" and unshelved.error is None
        await session.disconnect()

    async def test_collect_names_each_drawer_the_way_its_agent_does(self, agent_ws, authenticated_context):
        from facade.mutations import postman

        session = await open_agent(agent_ws, "pos-collect", session_id="proc-1")
        await session.send(messages.Shelve(ref="minted-c", identifier="@test/thing", resource_id="minted-c", **_j(1)))
        await _ack_until(session, 1)
        await session.send(messages.Shelve(ref="r-legacy", identifier="@test/thing", resource_id="legacy-c"))
        legacy_pk = (await session.receive(messages.Shelved)).drawer
        stranger = await sync_to_async(_stranger_drawer)("pos-collect-stranger")

        info = SimpleNamespace(context=authenticated_context)
        await sync_to_async(postman.collect)(info, inputs.CollectInputModel(drawers=["minted-c", legacy_pk, str(stranger.pk), stranger.resource_id]))
        collect = await session.receive(messages.Collect)
        assert sorted(collect.drawers) == sorted(["minted-c", legacy_pk])
        await session.disconnect()

    async def test_graphql_unshelve_accepts_a_resource_id_or_a_pk(self, agent_ws, authenticated_context):
        from facade.mutations.memory_shelve import UnshelveMemoryDrawerInput, unshelve_memory_drawer

        info = SimpleNamespace(context=authenticated_context)
        agent = await sync_to_async(_graphql_agent)(info)
        minted = await sync_to_async(_legacy_drawer)(agent.pk, "gql-minted", agent_minted=True)
        legacy = await sync_to_async(_legacy_drawer)(agent.pk, "gql-legacy")
        await sync_to_async(unshelve_memory_drawer)(info, UnshelveMemoryDrawerInput(id="gql-minted"))
        await sync_to_async(unshelve_memory_drawer)(info, UnshelveMemoryDrawerInput(id=str(legacy.pk)))
        assert not await MemoryDrawer.objects.filter(pk__in=[minted.pk, legacy.pk]).aexists()

    async def test_a_same_session_reconnect_keeps_the_drawers_and_a_new_session_clears_them(self, agent_ws):
        session = await open_agent(agent_ws, "pos-reconnect-shelf", session_id="proc-1")
        await session.send(messages.Shelve(ref="keep-1", identifier="@test/thing", resource_id="keep-1", **_j(1)))
        await _ack_until(session, 1)
        await session.disconnect()

        again = AgentSession(await agent_ws(), agent=session.agent)
        await again.register(token=TEST_TOKEN, force=True, session_id="proc-1")
        assert await MemoryDrawer.objects.filter(shelve__agent_id=session.agent_pk, resource_id="keep-1").aexists()
        await again.disconnect()

        fresh = AgentSession(await agent_ws(), agent=session.agent)
        await fresh.register(token=TEST_TOKEN, force=True, session_id="proc-2")
        assert not await MemoryDrawer.objects.filter(shelve__agent_id=session.agent_pk).aexists()
        await fresh.disconnect()

    async def test_a_refused_registration_does_not_clear_the_live_agents_drawers(self, agent_ws):
        session = await open_agent(agent_ws, "pos-refused", session_id="proc-1")
        await session.send(messages.Shelve(ref="live-1", identifier="@test/thing", resource_id="live-1", **_j(1)))
        await _ack_until(session, 1)

        intruder = AgentSession(await agent_ws(), agent=session.agent)
        await intruder.send(messages.Register(token=TEST_TOKEN, session_id="proc-9"))
        await intruder.receive(messages.ProtocolError)
        await intruder.expect_close(codes.AGENT_ALREADY_CONNECTED_CODE)
        assert await MemoryDrawer.objects.filter(resource_id="live-1").aexists()
        await session.disconnect()


def _legacy_drawer(agent_pk, resource_id: str, *, agent_minted: bool = False) -> MemoryDrawer:
    shelve = models.MemoryShelve.objects.get(agent_id=agent_pk)
    return MemoryDrawer.objects.create(shelve=shelve, resource_id=resource_id, identifier="@test/thing", agent_minted=agent_minted)


def _stranger_drawer(prefix: str) -> MemoryDrawer:
    """A drawer of an agent in another organization."""
    agent = _seed_throwaway_agent_graph(prefix)
    shelve, _ = models.MemoryShelve.objects.get_or_create(agent=agent, defaults=dict(name="s", description="", creator=agent.user, organization=agent.organization))
    return MemoryDrawer.objects.create(shelve=shelve, resource_id=f"{prefix}-drawer", identifier="@test/thing", agent_minted=True)


def _graphql_agent(info) -> models.Agent:
    from facade import registration

    request = info.context.request
    return registration.ensure_agent(request.client, request.user, request.organization)
