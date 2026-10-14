"""`Action.live_implementations` / `assignable`, kept by the database (migration ``0006``).

Writers leave marks; ``facade_refresh_live_implementations`` (takt's drain) recomputes. The
agent's lease is written the way takt writes it — the statements of
``takt/crates/facade/src/persist/leases.rs`` (claim, clean release, the stale sweep's revoke, the
heartbeat) — so these pin the triggers to what happens on a hub, not to a Django save. The
concurrency tests race a second, real connection against this one.

Another drainer may run beside these tests — in the full suite the session's real takt drains on
every notice — exactly as takt replicas do on a hub. So a test never asserts what one drain of its
own left behind, only what the count settles to (``settled``): a drain skips what another holds
mid-recompute, and may return before that one commits.
"""

import datetime
import time

import psycopg
import pytest
from django.db import connection
from django.utils import timezone

from facade import liveness, models
from rekuest_core.inputs.models import ImplementationInputModel

from tests.factories import create_agent_for_registry, create_registry_bundle
from tests.registered import create_implementation


def _input(key: str = "thresholder") -> ImplementationInputModel:
    return ImplementationInputModel.model_validate(
        {
            "interface": key,
            "definition": {"key": key, "version": "1", "name": key.title(), "kind": "FUNCTION", "args": [], "returns": []},
        }
    )


def _agent(prefix: str) -> models.Agent:
    user, _, org, caller = create_registry_bundle(prefix)
    return create_agent_for_registry(registry=caller, user=user, organization=org, prefix=prefix)


def _sql(statement: str, *params: object) -> None:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def drain(cursor=None) -> int:
    """takt's drain, until there is nothing left it can take."""
    total = 0
    for _ in range(10):
        if cursor is None:
            with connection.cursor() as own:
                own.execute("SELECT facade_refresh_live_implementations(500)")
                done = own.fetchone()[0]
        else:
            done = cursor.execute("SELECT facade_refresh_live_implementations(500)").fetchone()[0]
        total += done
        if done == 0:
            return total
    return total


def claim(agent: models.Agent, connection_id: str = "conn-1") -> None:
    """takt's lease claim (``on_agent_connected``)."""
    _sql(
        "UPDATE facade_agent SET connected = true, last_seen = %s, active_connection_id = %s WHERE id = %s",
        timezone.now(),
        connection_id,
        agent.id,
    )


def release(agent: models.Agent) -> None:
    """A clean close (``release_lease``)."""
    _sql("UPDATE facade_agent SET connected = false, last_seen = %s WHERE id = %s", timezone.now(), agent.id)


def revoke(agent: models.Agent) -> None:
    """The stale sweep (``revoke_lease``): ``last_seen`` is left alone."""
    _sql("UPDATE facade_agent SET connected = false, active_connection_id = NULL WHERE id = %s", agent.id)


def heartbeat(agent: models.Agent, connection_id: str = "conn-1") -> None:
    """The lease renewal (``renew_agent_lease``)."""
    _sql(
        "UPDATE facade_agent SET last_seen = %s WHERE id = %s AND active_connection_id = %s AND connected",
        timezone.now(),
        agent.id,
        connection_id,
    )


def live(action: models.Action) -> tuple[int, bool]:
    return models.Action.objects.values_list("live_implementations", "assignable").get(id=action.id)


def marks() -> int:
    return models.ActionLivenessDirty.objects.count()


def marks_ever() -> int:
    """How many marks were ever written: the log's sequence, which no drain moves back."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT last_value FROM facade_actionlivenessdirty_id_seq")
        return cursor.fetchone()[0]


def settled(*actions: models.Action, seconds: float = 5.0):
    """What the counts settle to: drain until no mark waits, then read them."""
    deadline = time.monotonic() + seconds
    while True:
        drain()
        if marks() == 0 or time.monotonic() > deadline:
            break
        time.sleep(0.02)
    values = [live(action) for action in actions]
    return values[0] if len(values) == 1 else values


def _other() -> psycopg.Connection:
    """A second connection to the test database, as another takt replica or writer would be."""
    settings = connection.settings_dict
    return psycopg.connect(
        dbname=settings["NAME"],
        user=settings["USER"],
        password=settings["PASSWORD"],
        host=settings["HOST"],
        port=settings["PORT"],
        autocommit=False,
    )


@pytest.mark.django_db(transaction=True)
class TestLiveImplementations:
    def test_follows_the_agent_through_connect_release_and_the_sweep(self):
        agent = _agent("live-lease")
        action = create_implementation(_input(), agent).action
        assert settled(action) == (0, False)

        claim(agent)
        assert settled(action) == (1, True)

        release(agent)
        assert settled(action) == (0, False)

        claim(agent, "conn-2")
        assert settled(action) == (1, True)
        revoke(agent)
        assert settled(action) == (0, False)

    def test_counts_every_agent_that_can_take_it(self):
        first, second = _agent("live-first"), _agent("live-second")
        action = create_implementation(_input(), first).action
        models.Implementation.objects.create(agent=second, action=action, interface="thresholder")

        claim(first)
        assert settled(action) == (1, True)
        claim(second)
        assert settled(action) == (2, True)
        release(first)
        assert settled(action) == (1, True)
        release(second)
        assert settled(action) == (0, False)

    def test_a_heartbeat_leaves_no_mark(self):
        agent = _agent("live-heartbeat")
        action = create_implementation(_input(), agent).action
        claim(agent)
        assert settled(action) == (1, True)
        before = marks_ever()
        heartbeat(agent)
        assert marks_ever() == before

    def test_follows_implementations_arriving_moving_and_leaving(self):
        online, offline, elsewhere = _agent("live-online"), _agent("live-offline"), _agent("live-elsewhere")
        claim(online)
        action = create_implementation(_input(), offline).action
        assert settled(action) == (0, False)

        implementation = models.Implementation.objects.create(agent=online, action=action, interface="thresholder-online")
        assert settled(action) == (1, True)

        models.Implementation.objects.filter(id=implementation.id).update(agent=elsewhere)
        assert settled(action) == (0, False)

        models.Implementation.objects.filter(id=implementation.id).update(agent=online)
        assert settled(action) == (1, True)

        implementation.delete()
        assert settled(action) == (0, False)

    def test_a_webhook_agent_is_always_available(self):
        agent = _agent("live-webhook")
        action = create_implementation(_input(), agent).action
        _sql("UPDATE facade_agent SET kind = 'WEBHOOK' WHERE id = %s", agent.id)
        assert settled(action) == (1, True)
        _sql("UPDATE facade_agent SET kind = 'WEBSOCKET' WHERE id = %s", agent.id)
        assert settled(action) == (0, False)

    def test_a_deleted_actions_mark_ends(self):
        agent = _agent("live-deleted")
        action = create_implementation(_input(), agent).action
        claim(agent)
        models.Action.objects.filter(id=action.id).delete()
        deadline = time.monotonic() + 5
        while marks() and time.monotonic() < deadline:
            drain()
            time.sleep(0.02)
        assert marks() == 0

    def test_no_code_writes_it(self):
        agent = _agent("live-guard")
        action = create_implementation(_input(), agent).action
        claim(agent)
        assert settled(action) == (1, True)
        stale = models.Action.objects.get(id=action.id)
        stale.live_implementations = 0
        stale.save()  # a full save of an instance holding another value
        assert live(action) == (1, True)

        models.Action.objects.filter(id=action.id).update(live_implementations=7)
        assert live(action) == (1, True)

        fresh = models.Action.objects.create(
            app=agent.app,
            organization=agent.organization,
            key="new",
            version="1",
            hash="new-action-hash",
            description="",
            live_implementations=3,
        )
        assert live(fresh) == (0, False)

    def test_the_python_rule_is_takts(self):
        agent = _agent("live-rule")
        claim(agent)
        available = models.Agent.objects.filter(liveness.agent_available_q())
        assert available.filter(id=agent.id).exists()

        stale = timezone.now() - datetime.timedelta(seconds=liveness.stale_after_seconds() + 1)
        _sql("UPDATE facade_agent SET last_seen = %s WHERE id = %s", stale, agent.id)
        assert not available.filter(id=agent.id).exists()

        _sql("UPDATE facade_agent SET kind = 'WEBHOOK', connected = false WHERE id = %s", agent.id)
        assert available.filter(id=agent.id).exists()


@pytest.mark.django_db(transaction=True)
class TestNoRaces:
    """Writers never wait on the drain, the drain never waits on anyone, and nothing is lost."""

    def test_a_change_not_yet_committed_is_counted_once_it_is(self):
        agent = _agent("race-uncommitted")
        action = create_implementation(_input(), agent).action
        assert settled(action) == (0, False)

        with _other() as writer:
            writer.execute("UPDATE facade_agent SET connected = true, last_seen = now() WHERE id = %s", [agent.id])
            # The writer's mark is its own until it commits: no drain, ours or another's, sees it.
            drain()
            assert live(action) == (0, False)
            writer.commit()

        assert settled(action) == (1, True)

    def test_an_action_someone_holds_is_skipped_without_waiting_and_kept_for_later(self):
        agent = _agent("race-held")
        action = create_implementation(_input(), agent).action
        assert settled(action) == (0, False)

        with _other() as holder:
            # Held before the change: no drainer can recompute it while this lasts.
            holder.execute("SELECT id FROM facade_action WHERE id = %s FOR UPDATE", [action.id])
            claim(agent)  # the writer is not held up: its trigger only appends a mark
            _sql("SET lock_timeout = '200ms'")
            try:
                drain()  # skips the held action instead of waiting: no lock_timeout error
            finally:
                _sql("SET lock_timeout = 0")
            assert live(action) == (0, False)
            holder.rollback()

        # The skipped mark was put back, so the count still arrives.
        assert settled(action) == (1, True)

    def test_two_drains_at_once_end_right(self):
        first, second = _agent("race-two-a"), _agent("race-two-b")
        actions = [create_implementation(_input(f"act{i}"), first).action for i in range(5)]
        for index, action in enumerate(actions):
            models.Implementation.objects.create(agent=second, action=action, interface=f"act{index}-b")
        assert settled(*actions) == [(0, False)] * 5
        claim(first)
        claim(second)

        with _other() as replica:
            # The replica takes some marks and holds them uncommitted while this drain runs.
            replica.execute("SELECT facade_refresh_live_implementations(3)")
            drain()
            replica.commit()
            drain(replica.cursor())
            replica.commit()

        assert settled(*actions) == [(2, True)] * 5
