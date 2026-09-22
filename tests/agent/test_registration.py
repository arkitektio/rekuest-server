"""Registration and shelving over the agent socket.

An agent needs no GraphQL to exist and to declare what it offers: one ``Register`` creates
it (with its memory shelve) and reconciles the implementations/states/locks/bloks it carries;
``Init`` answers with the definition hash the backend now holds and the registration's
diagnostics, or the connection is refused with a ``ProtocolError``. ``Shelve``/``Unshelve``
maintain the drawers of what it holds in memory.
"""

import pytest

from facade import messages
from facade.codes import AGENT_REGISTRATION_REJECTED_CODE
from facade.models import Agent, Implementation, Lock, MemoryDrawer, MemoryShelve, Snapshot, State
from tests.agent.helpers import connect_agent, open_agent
from tests.factories import TEST_TOKEN


def _implementation(interface: str, *, operation: str | None = None, keys=("a", "b")) -> dict:
    """A FUNCTION implementation; with ``operation`` its one arg carries a validator call."""
    arg = {"key": "exposure", "kind": "FLOAT", "nullable": False}
    if operation is not None:
        first, second = keys
        arg["validators"] = [{"call": {"operation": operation, "arguments": [{"key": first, "value_path": "/value"}, {"key": second, "value_literal": 0}]}, "source": f"{operation}(value, 0)"}]
    return {
        "interface": interface,
        "definition": {"key": interface, "version": "1", "name": interface.title(), "kind": "FUNCTION", "args": [arg], "returns": []},
    }


def _state(interface: str) -> dict:
    return {"interface": interface, "definition": {"name": interface, "ports": [{"key": "count", "kind": "INT", "nullable": False}]}}


async def _register_fresh(agent_ws, *, token: str = TEST_TOKEN, **declaration):
    """A bare connect + Register (optionally carrying a declaration): the socket creates the agent."""
    session = await connect_agent(agent_ws)
    await session.send(messages.Register(token=token, **declaration))
    session.init = await session.receive(messages.Init)
    return session


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestRegisterCreates:
    async def test_register_creates_the_agent_and_its_shelve(self, agent_ws):
        session = await _register_fresh(agent_ws)

        assert session.init.hash is None
        agent = await Agent.objects.select_related("app", "client").aget(pk=session.init.agent)
        assert agent.name == agent.client.client_id  # until its first Implement names it
        assert agent.app.identifier == "static_app"  # the static token's client release
        assert await MemoryShelve.objects.filter(agent=agent).aexists()
        assert await MemoryDrawer.objects.filter(shelve__agent=agent).acount() == 0

    async def test_init_reports_the_stored_hash(self, agent_ws):
        session = await open_agent(agent_ws, "hashed-agent")
        assert session.init.hash == "hashed-agent-hash"

    async def test_a_declaring_register_names_the_agent_and_a_bare_one_keeps_it(self, agent_ws):
        first = await _register_fresh(agent_ws, name="Fresh Agent", hash="n1")
        assert first.init.hash == "n1"
        await first.disconnect()

        second = await _register_fresh(agent_ws)
        agent = await Agent.objects.aget(pk=second.init.agent)
        assert agent.pk == int(first.init.agent) and agent.name == "Fresh Agent"
        assert second.init.hash == "n1"

    async def test_a_declaring_register_describes_the_agent(self, agent_ws):
        """A description reaches the row on exactly the registers a name does.

        It rides in the declaration, so a ``hash`` is sent with it: ``Register.declares``
        counts only hash/implementations/states/locks/bloks, and a Register carrying nothing
        else never reaches ``implement_agent`` at all.
        """
        session = await _register_fresh(agent_ws, name="Fresh Agent", description="The GPU box in the basement", hash="d1")

        agent = await Agent.objects.aget(pk=session.init.agent)
        assert agent.description == "The GPU box in the basement"

    async def test_a_register_that_omits_the_description_keeps_it(self, agent_ws):
        """Unlike ``name``, which a declaring register resets to the client id when omitted.

        A client that describes itself once should not lose that by shipping a build whose
        registration does not repeat it -- so an absent description means "unchanged", not
        "cleared".
        """
        first = await _register_fresh(agent_ws, description="The GPU box in the basement", hash="d2")
        await first.disconnect()

        second = await _register_fresh(agent_ws, implementations=[_implementation("scan")], hash="d3")
        agent = await Agent.objects.aget(pk=second.init.agent)
        assert agent.pk == int(first.init.agent)
        assert agent.description == "The GPU box in the basement"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestRegisterImplements:
    async def test_register_creates_implementations_states_and_locks(self, agent_ws):
        session = await _register_fresh(
            agent_ws,
            implementations=[_implementation("scan")],
            states=[_state("counter")],
            locks=[{"key": "stage", "definition": {"key": "stage", "description": "the stage"}}],
            hash="h1",
        )

        assert session.init.hash == "h1" and session.init.diagnostics == []
        assert await Implementation.objects.filter(agent_id=session.init.agent, interface="scan").aexists()
        assert await State.objects.filter(agent_id=session.init.agent, interface="counter").aexists()
        assert await Lock.objects.filter(agent_id=session.init.agent, key="stage").aexists()

        # The state row exists, so the state stream works without any GraphQL: a
        # SessionInit lands as a Snapshot.
        await session.send(messages.SessionInit(session_id="s-1", states={"counter": {"count": 1}}))
        assert await _eventually(lambda: Snapshot.objects.filter(state__agent_id=session.init.agent).aexists())

    async def test_a_matching_hash_skips_the_reconciliation(self, agent_ws):
        first = await _register_fresh(agent_ws, implementations=[_implementation("scan")], hash="h2")
        await first.disconnect()

        # Same hash, a different declaration: the backend trusts the hash and keeps what it has.
        again = await _register_fresh(agent_ws, implementations=[_implementation("other")], hash="h2")
        assert again.init.hash == "h2"
        assert await Implementation.objects.filter(agent_id=again.init.agent, interface="scan").aexists()
        assert not await Implementation.objects.filter(agent_id=again.init.agent, interface="other").aexists()

    async def test_a_new_hash_reconciles_and_reaps(self, agent_ws):
        first = await _register_fresh(agent_ws, implementations=[_implementation("scan")], hash="h3")
        await first.disconnect()

        again = await _register_fresh(agent_ws, implementations=[_implementation("other")], hash="h4")
        assert again.init.hash == "h4"
        assert not await Implementation.objects.filter(agent_id=again.init.agent, interface="scan").aexists()
        assert await Implementation.objects.filter(agent_id=again.init.agent, interface="other").aexists()

    async def test_init_carries_the_diagnostics(self, agent_ws):
        session = await _register_fresh(agent_ws, implementations=[_implementation("scan", operation="nonexistent_op")], hash="h5")

        (diagnostic,) = session.init.diagnostics
        assert diagnostic.code == "unknown_operation" and "'nonexistent_op'" in diagnostic.message

    async def test_a_catalog_mismatch_refuses_the_connection(self, agent_ws):
        # A base operation called with the wrong argument keys: nothing is stored, the agent
        # is told why, and the socket is closed with the registration code.
        session = await connect_agent(agent_ws)
        await session.send(messages.Register(token=TEST_TOKEN, implementations=[_implementation("scan", operation="between")], hash="h6"))

        error = await session.receive(messages.ProtocolError)
        assert "does not accept arguments" in error.error
        await session.expect_close(AGENT_REGISTRATION_REJECTED_CODE)
        assert await Implementation.objects.filter(agent__client__client_id="oinsoins").acount() == 0

        # The agent itself exists (ensure ran) and is free to register again.
        again = await _register_fresh(agent_ws, implementations=[_implementation("scan")], hash="h7")
        assert again.init.hash == "h7"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestShelve:
    async def test_shelve_and_unshelve_round_trip(self, agent_ws):
        session = await _register_fresh(agent_ws)

        await session.send(messages.Shelve(ref="r1", identifier="@test/thing", resource_id="thing-1", label="a thing"))
        shelved = await session.receive(messages.Shelved)
        assert shelved.ref == "r1" and shelved.error is None and shelved.drawer
        drawer = await MemoryDrawer.objects.select_related("shelve").aget(pk=shelved.drawer)
        assert drawer.shelve.agent_id == int(session.init.agent)
        assert drawer.identifier == "@test/thing" and drawer.resource_id == "thing-1" and drawer.label == "a thing"

        await session.send(messages.Unshelve(ref="r2", drawer=shelved.drawer))
        unshelved = await session.receive(messages.Unshelved)
        assert unshelved.ref == "r2" and unshelved.error is None
        assert not await MemoryDrawer.objects.filter(pk=shelved.drawer).aexists()

    async def test_unknown_drawer_answers_with_an_error(self, agent_ws):
        session = await _register_fresh(agent_ws)

        await session.send(messages.Unshelve(ref="r3", drawer="999999"))
        unshelved = await session.receive(messages.Unshelved)
        assert unshelved.ref == "r3" and "Unknown drawer" in (unshelved.error or "")

        await session.send(messages.Shelve(ref="r4", identifier="@test/thing", resource_id="thing-2"))
        assert (await session.receive(messages.Shelved)).error is None

    async def test_a_foreign_drawer_cannot_be_unshelved(self, agent_ws):
        owner = await _register_fresh(agent_ws, token=TEST_TOKEN)
        await owner.send(messages.Shelve(ref="r5", identifier="@test/thing", resource_id="thing-3"))
        drawer = (await owner.receive(messages.Shelved)).drawer

        other = await _register_fresh(agent_ws, token="test2")
        await other.send(messages.Unshelve(ref="r6", drawer=drawer))
        assert "does not belong" in ((await other.receive(messages.Unshelved)).error or "")
        assert await MemoryDrawer.objects.filter(pk=drawer).aexists()

    async def test_registering_again_forgets_the_drawers(self, agent_ws):
        session = await _register_fresh(agent_ws)
        await session.send(messages.Shelve(ref="r7", identifier="@test/thing", resource_id="thing-4"))
        await session.receive(messages.Shelved)
        await session.disconnect()

        again = await _register_fresh(agent_ws)
        assert await MemoryDrawer.objects.filter(shelve__agent_id=again.init.agent).acount() == 0


async def _eventually(predicate, *, tries: int = 50) -> bool:
    import asyncio

    for _ in range(tries):
        if await predicate():
            return True
        await asyncio.sleep(0.05)
    return await predicate()
