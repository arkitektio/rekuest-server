"""What breaks when more than one backend serves the same Postgres and redis.

Every test here reproduces an interleaving that cannot happen in a single process, so each one
passed vacuously before the multi-replica hardening. Two techniques, no mocks:

* **Two backend objects.** ``ModelPersistBackend()`` is stateless by design, so a second
  instance *is* a second backend as far as correctness goes: same DB, same redis, no shared
  memory. Anything that only works because one object handled both halves shows up here.
* **Real threads.** ``asyncio.gather`` cannot race the ORM — ``database_sync_to_async`` is
  thread-sensitive, so every call lands on one thread and one connection, and the "race" is
  serialized. Only real threads with their own connections race, and the interleaving is made
  deterministic by holding a row lock instead of sleeping.
"""

import asyncio
import threading
from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.db import connections, transaction
from django.utils import timezone

from facade import enums, inputs
from facade.backend import controll_backend
from facade.models import Agent, Task, TaskEvent
from facade.persist_backend import ModelPersistBackend

from tests.agent.helpers import open_agent
from tests.factories import _seed_throwaway_agent_graph as _seed_throwaway_agent, build_implementation_for_agent, build_task, seed_agent

pytestmark = pytest.mark.django_db(transaction=True)


def _build_higher_order_graph(agent_pk, prefix):
    """A lower action+impl and the higher-order impl wrapping it, both on ``agent``."""
    from facade.models import Action, Implementation

    agent = Agent.objects.select_related("app", "release", "organization").get(pk=agent_pk)
    lower_action = Action.objects.create(app=agent.app, key=f"{prefix}-lower", version="1.0.0", name="lower", description="lower", hash=f"{prefix}-lower-hash", organization=agent.organization)
    lower_impl = Implementation.objects.create(release=agent.release, interface=f"{prefix}-lower-iface", action=lower_action, agent=agent)
    higher_action = Action.objects.create(app=agent.app, key=f"{prefix}-higher", version="1.0.0", name="higher", description="higher", hash=f"{prefix}-higher-hash", organization=agent.organization)
    return Implementation.objects.create(
        release=agent.release,
        interface=f"{prefix}-higher-iface",
        action=higher_action,
        agent=agent,
        higher_order_for=lower_impl,
        higher_order_config={"args_key": "args", "return_map": {}},
    )


build_higher_order_graph = sync_to_async(_build_higher_order_graph)


def _build_wrapper_child_pair(prefix):
    """A wrapper task plus the child whose fate projects onto it (no agent socket needed)."""
    higher = _build_higher_order_graph(_seed_throwaway_agent(prefix).pk, prefix)
    wrapper = Task.objects.create(
        action=higher.action,
        agent=higher.agent,
        implementation=higher,
        latest_event_kind=enums.TaskEventKind.QUEUED,
        latest_instruct_kind=enums.TaskInstructChoices.ASSIGN,
    )
    child = Task.objects.create(
        action=higher.higher_order_for.action,
        agent=higher.agent,
        implementation=higher.higher_order_for,
        parent=wrapper,
        root=wrapper,
        is_higher_order_child=True,
        latest_event_kind=enums.TaskEventKind.STARTED,
        latest_instruct_kind=enums.TaskInstructChoices.ASSIGN,
    )
    return wrapper, child


build_wrapper_child_pair = sync_to_async(_build_wrapper_child_pair)


class _Info:
    def __init__(self, context):
        self.context = context


def _in_thread(fn):
    """Run ``fn`` on its own thread (hence its own DB connection) and return a joinable handle.

    Closing the connection is mandatory: a leaked one keeps the test database busy and turns
    the teardown warning into a failure.
    """
    box = {}

    def run():
        try:
            box["result"] = fn()
        except BaseException as error:  # noqa: BLE001 - re-raised by ``join``
            box["error"] = error
        finally:
            connections.close_all()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    def join(timeout=15):
        thread.join(timeout)
        assert not thread.is_alive(), "worker thread did not finish"
        if "error" in box:
            raise box["error"]
        return box.get("result")

    return join


def _assign_input(impl_pk, **kwargs):
    return inputs.AssignInputModel(implementation=str(impl_pk), args={}, **kwargs)


class TestAssignIsIdempotentAcrossBackends:
    """The dedupe on the caller-supplied reference is a *database* guarantee now.

    It used to be a read: ``filter(caller, reference).first()`` then create. Two retries of one
    assign — a flaky client, a retried HookAgent POST — reach two backends at the same instant,
    both read "absent", and both create a task and dispatch it. The work runs twice, which for
    physical-effect work is the worst outcome the system has.
    """

    @pytest.mark.asyncio
    async def test_the_loser_returns_the_winners_task_and_does_not_dispatch(self, agent_ws, authenticated_context, monkeypatch):
        session = await open_agent(agent_ws, "race-agent")
        impl = await build_implementation_for_agent(session.agent.pk, "race")
        context, impl_pk = authenticated_context, impl.pk

        # Count the recoveries so the test cannot pass by accident: if the worker's fast-path read
        # happened to run after the commit it would also return the same task, but WITHOUT the
        # constraint having fired — and that is the guarantee under test.
        recoveries = []
        real_recover = type(controll_backend)._lost_reference_race

        def counting_recover(caller, reference):
            recoveries.append(reference)
            return real_recover(caller, reference)

        monkeypatch.setattr(type(controll_backend), "_lost_reference_race", staticmethod(counting_recover))

        def other_backend_assigns():
            # A second "replica": its own thread, its own connection, no shared state.
            return controll_backend.assign_with_status(_Info(context), _assign_input(impl_pk, reference="shared-ref"))

        # Hold the row uncommitted so the worker is guaranteed to be inside the window: under
        # READ COMMITTED its fast-path read cannot see our row, so it proceeds to INSERT and
        # blocks on the unique index until we commit.
        def win_the_race():
            with transaction.atomic():
                mine, created = controll_backend.assign_with_status(_Info(context), _assign_input(impl_pk, reference="shared-ref"))
                assert created is True
                join = _in_thread(other_backend_assigns)
                # Give the worker time to reach the blocking INSERT before we commit.
                import time

                time.sleep(1.0)
                return mine, join

        winner, join = await sync_to_async(win_the_race)()
        loser, created = await sync_to_async(join)()

        assert recoveries == ["shared-ref"], "the loser must have been rejected by the unique constraint"
        assert created is False, "the backend that lost the race must not claim it created the task"
        assert loser.pk == winner.pk, "the loser must return the winner's task, not a second one"
        assert await Task.objects.filter(reference="shared-ref").acount() == 1

        # And exactly ONE Assign frame reached the executor.
        frame = await session.communicator.receive_json_from(timeout=5)
        assert frame["type"] == "ASSIGN" and frame["task"] == str(winner.pk)
        assert await session.communicator.receive_nothing(timeout=0.5)

    @pytest.mark.asyncio
    async def test_a_lost_race_on_a_wrapper_leaves_no_orphan_child(self, agent_ws, authenticated_context):
        """A higher-order assign creates wrapper + child in one transaction. The wrapper carries
        the caller's reference, so a lost race must roll back the pair — a child whose wrapper
        belongs to someone else's assign could never be finished by anyone."""
        session = await open_agent(agent_ws, "race-ho-agent")
        higher = await build_higher_order_graph(session.agent.pk, "race-ho")
        context, higher_pk = authenticated_context, higher.pk

        def other_backend_assigns():
            return controll_backend.assign_with_status(_Info(context), _assign_input(higher_pk, reference="shared-ho-ref"))

        def win_the_race():
            with transaction.atomic():
                mine, _ = controll_backend.assign_with_status(_Info(context), _assign_input(higher_pk, reference="shared-ho-ref"))
                join = _in_thread(other_backend_assigns)
                import time

                time.sleep(1.0)
                return mine, join

        winner, join = await sync_to_async(win_the_race)()
        loser, created = await sync_to_async(join)()

        assert created is False and loser.pk == winner.pk
        # One wrapper, and exactly one child — the loser's child was rolled back with it.
        assert await Task.objects.filter(reference="shared-ho-ref").acount() == 1
        assert await Task.objects.filter(parent=winner).acount() == 1


class TestLeaseIsNeverUndoneByAnotherBackend:
    """``lease_epoch`` is the fencing token: it says which connection may execute an agent's work.
    Anything that rewrites it from a stale snapshot un-fences a connection whose work was
    already failed, and the agent resurrects itself on its next heartbeat."""

    async def _claimed(self, prefix):
        agent = await seed_agent(f"{prefix}-agent")
        claim = await ModelPersistBackend().on_agent_connected(agent.pk, "c1", session_id="S1")
        assert claim.claimed
        return await Agent.objects.aget(pk=agent.pk)

    @pytest.mark.asyncio
    async def test_a_stale_bare_save_does_not_touch_the_lease(self):
        agent = await self._claimed("stale-save")
        stale = await Agent.objects.aget(pk=agent.pk)  # the snapshot an operator's request holds

        # The connection wedged (a killed worker): its lease expires without anyone releasing it,
        # so another backend's sweep revokes it — ``connected=False`` plus an epoch bump that
        # fences the wedged connection's next heartbeat.
        await Agent.objects.filter(pk=agent.pk).aupdate(last_seen=timezone.now() - timedelta(hours=1))
        assert await sync_to_async(ModelPersistBackend()._revoke_lease_sync)(agent.pk) is True
        revoked = await Agent.objects.aget(pk=agent.pk)
        assert revoked.connected is False and revoked.lease_epoch > agent.lease_epoch

        # …and the request finally saves its stale row (the historic ``agent.save()``).
        stale.blocked = True
        await sync_to_async(stale.save)()

        after = await Agent.objects.aget(pk=agent.pk)
        assert after.blocked is True, "the operator's own change must still land"
        assert after.lease_epoch == revoked.lease_epoch, "a bare save must not un-bump the fencing token"
        assert after.connected is False, "…nor resurrect a revoked agent"

    @pytest.mark.asyncio
    async def test_a_late_disconnect_does_not_release_a_reclaimed_lease(self):
        """The old connection's teardown races the agent's reconnect onto another backend."""
        agent = await self._claimed("late-disconnect")
        running = await build_task("late-disconnect-task", agent_pk=agent.pk)

        # The agent reconnects elsewhere (new connection id) BEFORE the old socket's teardown
        # runs. ``force`` is what a client sends to take its own agent back from a connection it
        # believes is gone — the incumbent still looks live here precisely because nobody has
        # processed its disconnect yet.
        claim = await ModelPersistBackend().on_agent_connected(agent.pk, "c2", session_id="S1", force=True)
        assert claim.claimed

        # Now the displaced connection tears down. It must touch nothing.
        await ModelPersistBackend().on_agent_disconnected(agent.pk, "c1")

        after = await Agent.objects.aget(pk=agent.pk)
        assert after.connected is True, "the live holder must stay connected"
        assert after.active_connection_id == "c2"
        assert (await Task.objects.aget(pk=running.pk)).is_done is False


class TestExactlyOneTerminalEventPerTask:
    @pytest.mark.asyncio
    async def test_concurrent_wrapper_unfolds_produce_one_terminal(self):
        """A higher-order wrapper is finalized by whoever finalizes its child — an agent report on
        one backend, a sweep on another. That write used to bypass the row-locked claim."""
        wrapper, child = await build_wrapper_child_pair("unfold-race")

        await asyncio.gather(*(ModelPersistBackend()._unfold_to_higher_order(str(child.pk), enums.TaskEventKind.COMPLETED) for _ in range(4)))

        kinds = [e.kind async for e in TaskEvent.objects.filter(task_id=wrapper.pk)]
        assert kinds.count(enums.TaskEventKind.COMPLETED) == 1
        assert (await Task.objects.aget(pk=wrapper.pk)).is_done is True

    @pytest.mark.asyncio
    async def test_a_late_yield_cannot_reopen_a_finished_wrapper(self):
        wrapper, child = await build_wrapper_child_pair("unfold-late")
        await ModelPersistBackend()._unfold_to_higher_order(str(child.pk), enums.TaskEventKind.COMPLETED)

        await ModelPersistBackend()._unfold_to_higher_order(str(child.pk), enums.TaskEventKind.YIELD, returns={})

        refreshed = await Task.objects.aget(pk=wrapper.pk)
        assert refreshed.is_done is True
        assert refreshed.latest_event_kind == enums.TaskEventKind.COMPLETED


class TestRevisionOrdersTheChangeFeed:
    """Change payloads are produced by several backends and the channel layer does not deliver in
    commit order, so a consumer needs something monotonic to compare. ``updated_at`` could not
    serve: ``auto_now`` is skipped whenever ``update_fields`` omits it, which is how almost every
    task write is made — it was frozen at creation."""

    @pytest.mark.asyncio
    async def test_every_row_write_bumps_it_and_the_payload_carries_the_fresh_value(self):
        from facade import channel_events

        task = await build_task("rev")
        assert task.revision == 1
        seen = []

        def record(sender, instance, **kwargs):
            # What ``task_post_save`` builds the change payload from.
            payload = channel_events.TaskChangePayload.from_task(instance)
            seen.append(payload.revision)

        from django.db.models.signals import post_save

        post_save.connect(record, sender=Task)
        try:
            backend = ModelPersistBackend()
            await backend._claim(task.pk, to_kind=enums.TaskEventKind.STARTED, event={})
            await backend._claim(task.pk, to_kind=enums.TaskEventKind.COMPLETED, mark_done=True, event={})
        finally:
            post_save.disconnect(record, sender=Task)

        assert all(isinstance(r, int) for r in seen), f"payload must carry the integer, not an F() expression: {seen}"
        assert seen == sorted(seen) and len(set(seen)) == len(seen), f"revisions must be strictly increasing: {seen}"
        refreshed = await Task.objects.aget(pk=task.pk)
        assert refreshed.revision > 1
        assert refreshed.updated_at > refreshed.created_at, "updated_at must move with the row"

    @pytest.mark.asyncio
    async def test_two_backends_writing_get_distinct_revisions(self):
        task = await build_task("rev-race")
        await asyncio.gather(*(ModelPersistBackend()._claim(task.pk, to_kind=enums.TaskEventKind.PROGRESS, event={}) for _ in range(5)))
        # Only the first claim wins (the rest see the same kind), but the count must never regress.
        assert (await Task.objects.aget(pk=task.pk)).revision >= 2


class TestProbeIndexSurvivesAReconnectElsewhere:
    @pytest.mark.asyncio
    async def test_a_late_teardown_does_not_forget_probes_registered_since(self, agent_ws_redis):
        """``fail_all_for_agent`` used to DELETE the agent's whole live-probe index. If the agent
        had meanwhile reconnected — here or on another backend — and registered new probes, those
        lost their index entry: no later disconnect fails them, their terminal claim never runs,
        and the caller's in-flight slots leak until it is refused new probes altogether."""
        from facade.probes.persist import ProbeEventBackend
        from facade.probes.store import ProbeStore

        agent = await seed_agent("probe-index-agent")

        def register(store, probe_id):
            store.create(
                probe_id,
                agent_pk=agent.pk,
                caller_pk=7,
                user_sub="u",
                org_slug="o",
                action_pk=2,
                implementation_pk=3,
                interface="iface",
                reference=None,
            )

        class ReconnectsMidTeardown(ProbeStore):
            """The real store, with the interleaving pinned: the agent's reconnect lands in the
            window between the cascade's scan of the index and its cleanup of it."""

            async def live_calls_for_agent(self, agent_pk):
                live = await super().live_calls_for_agent(agent_pk)
                await sync_to_async(register)(self, "p-brand-new")
                return live

        store = ReconnectsMidTeardown.from_settings()
        await sync_to_async(register)(store, "p-old-one")
        await sync_to_async(register)(store, "p-old-two")

        assert await ProbeEventBackend(store=store).fail_all_for_agent(agent.pk) == 2

        still_indexed = await store.live_calls_for_agent(agent.pk)
        assert "p-brand-new" in still_indexed, "a probe registered after the scan must stay indexed"
        assert "p-old-one" not in still_indexed and "p-old-two" not in still_indexed


class TestReaperIsSafeFromEveryBackend:
    @pytest.mark.asyncio
    async def test_concurrent_full_passes_heal_once(self, settings):
        """Every backend runs the reaper; the redis tick token only de-duplicates work, so
        correctness must not depend on it. Run four passes at once and require one outcome."""
        from facade.reaper import run_sweeps

        settings.REKUEST_GRACE = {"DEFAULT": 0.05, "PHYSICAL": 0.05, "DISCONNECTED_EXPIRY": 3600}
        task = await build_task("reap-race")
        await Agent.objects.filter(pk=task.agent_id).aupdate(connected=False, last_seen=timezone.now() - timedelta(minutes=5))

        await asyncio.gather(*(run_sweeps() for _ in range(4)))

        kinds = [e.kind async for e in TaskEvent.objects.filter(task_id=task.pk)]
        assert kinds.count(enums.TaskEventKind.DISCONNECTED) == 1


class TestControlDeadlineIsOnByDefault:
    def test_an_unconfirmed_cancel_is_no_longer_left_forever(self):
        """A Cancel frame can be lost (a displaced connection, a redis restart) — and unlike an
        Assign nothing redelivers it. With the deadline off, the DB said CANCELLING while the
        agent had never heard of the task."""
        from rekuest.configuration import RekuestBlock

        assert RekuestBlock().control_deadline > 0


class TestClockSkewGate:
    """Across hosts, ``facade.liveness`` compares one backend's clock to another's writes. A
    backend whose clock runs ahead sees healthy agents as stale, revokes their leases and fails
    their work — then the agent reconnects and it happens again. So a drifted backend refuses to
    sweep and reports unhealthy, instead of quietly deciding other backends' agents are dead."""

    @pytest.mark.asyncio
    async def test_the_margin_is_derived_from_the_heartbeat_settings(self, settings):
        from facade import clock

        settings.AGENT_STALE_AFTER = 30
        settings.AGENT_HEARTBEAT_INTERVAL = 10
        settings.AGENT_HEARTBEAT_RESPONSE_TIMEOUT = 5
        # A fresh heartbeat is already up to (interval + response timeout) old, so only the rest
        # of the stale window is budget — halved, because two backends can drift oppositely.
        assert clock.max_skew_seconds() == 7.5

    @pytest.mark.asyncio
    async def test_a_skewed_backend_does_not_sweep(self, settings, monkeypatch):
        from facade import clock, reaper

        settings.REKUEST_GRACE = {"DEFAULT": 0.05, "PHYSICAL": 0.05}
        task = await build_task("skew-guard")
        await Agent.objects.filter(pk=task.agent_id).aupdate(connected=False, last_seen=timezone.now() - timedelta(minutes=5))

        monkeypatch.setattr(clock, "measure_skew", lambda: clock.max_skew_seconds() * 4)
        await reaper.run_sweeps()
        assert (await Task.objects.aget(pk=task.pk)).latest_event_kind != enums.TaskEventKind.DISCONNECTED

        # A correctly-clocked backend still acts on it — the deadline lives in the DB.
        monkeypatch.setattr(clock, "measure_skew", lambda: 0.0)
        await reaper.run_sweeps()
        assert (await Task.objects.aget(pk=task.pk)).latest_event_kind == enums.TaskEventKind.DISCONNECTED

    @pytest.mark.asyncio
    async def test_an_unmeasurable_skew_never_blocks_work(self, monkeypatch):
        from facade import clock

        def explode():
            raise RuntimeError("no database")

        monkeypatch.setattr(clock, "measure_skew", explode)
        assert clock.check_skew() is None  # the DB health check next to it owns that failure


class TestFleetRegistrationDoesNotDeadlockOrAbort:
    """A rollout restarts every worker of an app at once, so N agents register simultaneously
    against N backends. They share their app's ``Action`` rows — and, through protocol inference,
    ``Protocol``/``Collection`` rows too. Two registrations therefore used to lock those shared
    rows in whatever order their declarations listed them (a lock-order inversion Postgres
    resolves by aborting one side) and, on a genuinely new action, could both reach a bare
    ``create`` and have one abort on the unique constraint. Either way a worker came up
    unregistered and retried into the same race."""

    @staticmethod
    def _declaration(interface, key):
        return {
            "interface": interface,
            "definition": {"key": key, "version": "1", "name": key.title(), "kind": "FUNCTION", "args": [], "returns": []},
        }

    def _register(self, client, user, org, declarations):
        from types import SimpleNamespace

        from facade.mutations.agent import ImplementAgentInputModel, implement_agent

        payload = ImplementAgentInputModel.model_validate({"implementations": declarations})
        info = SimpleNamespace(context=SimpleNamespace(request=SimpleNamespace(client=client, user=user, organization=org)))
        return implement_agent(info, SimpleNamespace(to_pydantic=lambda: payload))

    def test_two_workers_of_one_fleet_register_concurrently(self):
        from authentikate.models import App, Client, Device, Organization, Release, User

        from facade.models import Action, Implementation

        org = Organization.objects.create(slug="fleet-org")
        app = App.objects.create(identifier="fleet-app")
        release = Release.objects.create(app=app, version="1.0.0")
        workers = []
        for index in (1, 2):
            user = User.objects.create(username=f"fleet-u{index}", password="x", sub=f"fleet-sub-{index}")
            device = Device.objects.create(device_id=f"fleet-dev-{index}")
            client = Client.objects.create(client_id=f"fleet-client-{index}", device=device)
            client.release = release
            client.save()
            workers.append((client, user))

        # The same two NEW shared actions, declared in OPPOSITE order by the two workers: the
        # interleaving that inverts the lock order.
        shared = [self._declaration("alpha", "shared-alpha"), self._declaration("beta", "shared-beta")]
        first = _in_thread(lambda: self._register(workers[0][0], workers[0][1], org, shared))
        second = _in_thread(lambda: self._register(workers[1][0], workers[1][1], org, list(reversed(shared))))

        first(), second()  # neither may raise (IntegrityError / deadlock detected)

        # One Action per (key, version) for the app, shared by both agents' implementations.
        assert Action.objects.filter(app=app, key="shared-alpha").count() == 1
        assert Action.objects.filter(app=app, key="shared-beta").count() == 1
        assert Implementation.objects.filter(action__app=app).count() == 4  # 2 agents × 2 actions

    def test_a_blok_declared_by_two_agents_gets_one_materialization_each(self):
        """``update_or_create(blok=…)`` made two agents fight over one row, and a single
        hand-made materialization of the same blok broke registration permanently."""
        from facade.models import Blok, MaterializedBlok, UICatalog

        agent_one = _seed_throwaway_agent("blok-one")
        agent_two = _seed_throwaway_agent("blok-two")
        catalog = UICatalog.objects.create(name="shared-catalog", organization=agent_one.organization)
        blok = Blok.objects.create(name="shared-blok", organization=agent_one.organization, description="d", creator=agent_one.user, catalog=catalog)
        hand_made = MaterializedBlok.objects.create(blok=blok, name="a user's own", description="")

        for agent in (agent_one, agent_two):
            MaterializedBlok.objects.update_or_create(blok=blok, declared_by=agent, defaults=dict(name=blok.name, description=""))
            MaterializedBlok.objects.update_or_create(blok=blok, declared_by=agent, defaults=dict(name=blok.name, description=""))

        assert MaterializedBlok.objects.filter(blok=blok, declared_by__isnull=False).count() == 2
        assert MaterializedBlok.objects.filter(pk=hand_made.pk).exists(), "a hand-made materialization must be untouched"


class TestMigrateIsSerialized:
    """Every replica runs ``manage.py migrate`` at boot — there is deliberately no separate
    migration job to operate. Django does not serialize that: two replicas both read
    ``django_migrations``, both decide the same migration is unapplied, and one dies on
    ``relation already exists`` (under ``set -e``, without ever starting daphne)."""

    def test_a_second_migrate_waits_for_the_holder(self):
        import time

        from django.core.management import call_command
        from django.db import connection as default_connection

        from facade.management.commands.migrate import LOCK_KEY

        # Stand in for the replica that is already migrating: hold the lock on its own session.
        holder = default_connection.copy()  # a separate connection, not this thread's
        holder.connect()
        try:
            with holder.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", [LOCK_KEY])

            started = time.monotonic()
            finished = []

            def migrate():
                call_command("migrate", verbosity=0)
                finished.append(time.monotonic())

            join = _in_thread(migrate)
            time.sleep(1.0)
            assert not finished, "migrate must wait while another replica holds the lock"

            with holder.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_KEY])
            join()

            assert finished and finished[0] - started >= 1.0
        finally:
            holder.close()

    def test_no_lock_is_available_as_an_escape_hatch(self):
        from facade.management.commands.migrate import Command

        parser = Command().create_parser("manage.py", "migrate")
        options = vars(parser.parse_args(["--no-lock"]))
        assert options["no_lock"] is True


class TestLoadersAreRequestScoped:
    """The dependency resolvers' DataLoaders used to be module-level with caching on. A
    ``DataLoader``'s cache is never evicted, so each process pinned the first row it ever loaded
    for an id — an agent's name or ``connected`` frozen at some arbitrary past moment, for the life
    of the process. With several backends it is also inconsistent: which stale snapshot you get
    depends on which replica answered."""

    @staticmethod
    def _info(context_type="http"):
        from types import SimpleNamespace

        return SimpleNamespace(context=SimpleNamespace(_loaders={}, type=context_type))

    @pytest.mark.asyncio
    async def test_a_later_request_sees_an_updated_agent(self):
        from facade import loaders

        agent = await seed_agent("loader-agent")
        first = self._info()
        assert (await loaders.agent_loader(first).load(str(agent.pk))).name == agent.name

        await Agent.objects.filter(pk=agent.pk).aupdate(name="renamed elsewhere")

        second = self._info()  # a new request ⇒ a new loader
        assert (await loaders.agent_loader(second).load(str(agent.pk))).name == "renamed elsewhere"

    @pytest.mark.asyncio
    async def test_batching_still_dedupes_within_one_request(self):
        from facade import loaders

        await seed_agent("loader-batch")
        info = self._info()
        loader = loaders.agent_loader(info)
        assert loader is loaders.agent_loader(info), "one loader per request, or batching is lost"
        assert loader.cache is True  # an HTTP context is per-request, so caching is safe there

    @pytest.mark.asyncio
    async def test_a_subscription_never_caches(self):
        """A websocket context lives as long as the subscription — possibly days — so a cache on
        it is the original bug with extra steps."""
        from facade import loaders

        assert loaders.agent_loader(self._info("ws")).cache is False


class TestAgentMutationsAreOrgScoped:
    """``pin``/``update``/``delete`` resolved the agent by id alone, so any authenticated user
    could rename or delete another organization's production agent by naming its id."""

    @pytest.mark.asyncio
    async def test_naming_another_organizations_agent_is_refused(self, authenticated_context):
        from types import SimpleNamespace

        from facade import inputs as facade_inputs
        from facade.mutations import agent as agent_mutations

        stranger = await sync_to_async(_seed_throwaway_agent)("tenant-stranger")
        info = SimpleNamespace(context=authenticated_context)

        for call in (
            lambda: agent_mutations.update_agent(info, facade_inputs.UpdateAgentInput(id=str(stranger.pk), name="pwned")),
            lambda: agent_mutations.delete_agent(info, agent_mutations.DeleteAgentInput(id=str(stranger.pk))),
            lambda: agent_mutations.pin_agent(info, facade_inputs.PinInput(id=str(stranger.pk), pin=True)),
        ):
            with pytest.raises(PermissionError):
                await sync_to_async(call)()

        assert (await Agent.objects.aget(pk=stranger.pk)).name == stranger.name
