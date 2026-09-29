"""WebSocket helpers shared by the full-stack agent tests.

``AgentSession`` wraps a Channels ``WebsocketCommunicator`` with a small typed API so tests
read by intent: ``await session.send(message)`` / ``msg = await session.receive(MessageClass)``
(a *parsed* pydantic message — assert on ``msg.progress``, not ``frame["progress"]``) /
``await session.expect_close(code)``. ``open_agent`` seeds + connects + registers and hands
back the seeded ``Agent`` on ``session.agent``.

The free ``register`` / ``send_message`` / ``connect_and_register`` functions are kept for the
non-agent tests that import them (``test_orchestration`` / ``test_provenance_dispatch``). The
unit-only fakes (``FakeAgent`` / ``FakeBackend`` / ``make_protocol``) live in
``test_protocol_unit.py``.
"""

from typing import Optional, Type, TypeVar

from facade import messages

from tests.factories import TEST_TOKEN, seed_agent

RECEIVE_TIMEOUT = 5

M = TypeVar("M", bound=messages.Message)


def _type_value(message_cls: Type[messages.Message]) -> str:
    """The wire ``type`` string a message class serializes to (e.g. ``"PROGRESS_EVENT"``)."""
    default = message_cls.model_fields["type"].default
    return getattr(default, "value", default)


class AgentSession:
    """A typed wrapper around one agent ``WebsocketCommunicator``."""

    def __init__(self, communicator, agent=None) -> None:
        self.communicator = communicator
        self.agent = agent  # the seeded Agent model (None for a bare connect)
        self.init: Optional[messages.Init] = None

    @property
    def agent_pk(self):
        if self.agent is not None:
            return self.agent.pk
        return self.init.agent if self.init is not None else None

    async def send(self, message: messages.Message) -> None:
        """Send a pydantic FromAgent message over the socket."""
        await self.communicator.send_to(text_data=message.model_dump_json())

    async def send_raw(self, text: str) -> None:
        """Send a raw frame (for the invalid-JSON / unknown-type protocol tests)."""
        await self.communicator.send_to(text_data=text)

    async def receive(self, message_cls: Type[M], *, tries: int = 12) -> M:
        """Return the next frame of ``message_cls``'s type, parsed — skipping others (e.g. heartbeats)."""
        target = _type_value(message_cls)
        for _ in range(tries):
            frame = await self.communicator.receive_json_from(timeout=RECEIVE_TIMEOUT)
            if frame.get("type") == target:
                return message_cls.model_validate(frame)
        raise AssertionError(f"did not receive a {message_cls.__name__} ({target}) frame")

    async def register(self, *, token: str = TEST_TOKEN, force: bool = False, session_id: Optional[str] = None) -> messages.Init:
        """Happy-path Register → parsed ``Init`` (also stored on ``.init``)."""
        await self.send(messages.Register(token=token, force=force, session_id=session_id))
        self.init = await self.receive(messages.Init)
        return self.init

    async def expect_close(self, code: int) -> None:
        """Assert the socket's next output is a ``websocket.close`` with ``code``."""
        output = await self.communicator.receive_output(timeout=RECEIVE_TIMEOUT)
        assert output["type"] == "websocket.close", output
        assert output["code"] == code, output

    async def disconnect(self) -> None:
        await self.communicator.disconnect()


async def connect_agent(agent_ws) -> AgentSession:
    """Bare connect — no seed, no register (for protocol-violation / second-connection tests)."""
    return AgentSession(await agent_ws())


async def open_agent(agent_ws, name: str, *, token: str = TEST_TOKEN, register: bool = True, session_id: Optional[str] = None, **seed_kwargs) -> AgentSession:
    """Seed the agent, connect, (optionally) register; return a session with ``.agent`` set.

    ``token`` selects the identity (a different static token → a different Agent), enabling
    cross-agent tests where one agent assigns to another.
    """
    agent = await seed_agent(name, token=token, **seed_kwargs)
    session = AgentSession(await agent_ws(), agent=agent)
    if register:
        await session.register(token=token, session_id=session_id)
    return session


# --------------------------------------------------------------------------- #
# Legacy free helpers — kept for the non-agent tests that import them.
# --------------------------------------------------------------------------- #
async def register(communicator, instance_id="test-agent", token=TEST_TOKEN, force=False):
    """Send a ``Register`` and return the parsed ``Init`` response payload (raw dict)."""
    await communicator.send_to(text_data=messages.Register(token=token, force=force).model_dump_json())
    return await communicator.receive_json_from(timeout=RECEIVE_TIMEOUT)


async def send_message(communicator, message):
    """Serialize a pydantic message and send it over the socket."""
    await communicator.send_to(text_data=message.model_dump_json())


async def connect_and_register(agent_ws, instance_id, **seed_kwargs):
    """Seed the agent, connect, register, and return ``(communicator, init)``."""
    await seed_agent(instance_id, **seed_kwargs)
    communicator = await agent_ws()
    init = await register(communicator, instance_id=instance_id)
    return communicator, init
