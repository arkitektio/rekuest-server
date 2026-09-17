"""Unit tests for the redis probe store — the concurrency primitives Probes rely on.

Runs against the dokker redis (:6666). No Django models are involved; ``backend_stack``
only guarantees the redis container is up.
"""

import asyncio

import pytest
import redis as sync_redis
from django.conf import settings

from facade.probes.ids import is_probe_id, new_probe_id
from facade.probes.store import ProbeStore, _agent_index_key, _call_key, probe_linger_seconds, probe_ttl_seconds


@pytest.fixture()
def store(backend_stack):
    client = sync_redis.Redis(host=settings.AGENT_REDIS_HOST, port=settings.AGENT_REDIS_PORT)
    client.flushdb()
    client.close()
    return ProbeStore.from_settings()


def _create(store, probe_id, agent_pk=1, caller_pk=7):
    return store.create(
        probe_id,
        agent_pk=agent_pk,
        caller_pk=caller_pk,
        user_sub="u",
        org_slug="o",
        action_pk=2,
        implementation_pk=3,
        interface="iface",
        reference=None,
    )


class TestCallIds:
    def test_prefix_is_unambiguous(self):
        probe_id = new_probe_id()
        assert is_probe_id(probe_id)
        assert not is_probe_id("123")  # a Task PK
        assert not is_probe_id(123)
        assert not is_probe_id(None)


class TestProbeStore:
    def test_create_sets_state_ttl_and_agent_index(self, store):
        probe_id = new_probe_id()
        _create(store, probe_id, agent_pk=11)

        state = store.get(probe_id)
        assert state["kind"] == "QUEUED"
        assert state["seq"] == "0"
        assert "done" not in state

        client = sync_redis.Redis(host=settings.AGENT_REDIS_HOST, port=settings.AGENT_REDIS_PORT, decode_responses=True)
        ttl = client.ttl(_call_key(probe_id))
        assert 0 < ttl <= probe_ttl_seconds()
        assert probe_id in client.smembers(_agent_index_key(11))
        client.close()

    def test_seq_is_monotonic_across_writes(self, store):
        probe_id = new_probe_id()
        _create(store, probe_id)

        async def drive():
            seqs = []
            for _ in range(5):
                seq, caller, origin = await store.record_nonterminal(probe_id, "PROGRESS")
                assert caller == "7" and origin == "graphql"
                seqs.append(seq)
            return seqs

        assert asyncio.run(drive()) == [1, 2, 3, 4, 5]

    def test_terminal_claim_has_exactly_one_winner(self, store):
        probe_id = new_probe_id()
        _create(store, probe_id)

        async def drive():
            results = await asyncio.gather(*(store.claim_terminal(probe_id, "COMPLETED") for _ in range(8)))
            return [r for r in results if r is not None]

        winners = asyncio.run(drive())
        assert len(winners) == 1
        seq, state = winners[0]
        assert state["done"] == "COMPLETED"

        # terminal reduces the TTL to the linger window and drops the agent index entry
        client = sync_redis.Redis(host=settings.AGENT_REDIS_HOST, port=settings.AGENT_REDIS_PORT, decode_responses=True)
        assert 0 < client.ttl(_call_key(probe_id)) <= probe_linger_seconds()
        assert client.smembers(_agent_index_key(1)) == set()
        client.close()

    def test_events_after_terminal_are_dropped(self, store):
        probe_id = new_probe_id()
        _create(store, probe_id)

        async def drive():
            await store.claim_terminal(probe_id, "COMPLETED")
            return await store.record_nonterminal(probe_id, "PROGRESS")

        assert asyncio.run(drive()) is None

    def test_unknown_call_never_resurrects(self, store):
        probe_id = new_probe_id()

        async def drive():
            return (
                await store.record_nonterminal(probe_id, "PROGRESS"),
                await store.claim_terminal(probe_id, "COMPLETED"),
            )

        assert asyncio.run(drive()) == (None, None)
        assert store.get(probe_id) is None

    def test_inflight_cap(self, store):
        from django.test import override_settings

        with override_settings(PROBE_MAX_INFLIGHT_PER_CALLER=2):
            assert store.try_acquire_slot("cap-caller")
            assert store.try_acquire_slot("cap-caller")
            assert not store.try_acquire_slot("cap-caller")  # over the cap — refused
            # the refused acquire must not leak a slot: releasing one frees exactly one
            store.release_slot_sync("cap-caller")
            assert store.try_acquire_slot("cap-caller")
            assert not store.try_acquire_slot("cap-caller")
