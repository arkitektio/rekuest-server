"""Registering is implementing: what a REGISTER declares is reconciled before the lease is claimed."""

import copy
import uuid

from conftest import ECHO_DECLARATION, session_id

SCHEMA = 3003
REGISTRATION_REJECTED = 4006


def declaration(**implementation: object) -> dict:
    """The echo declaration, its one implementation patched (``definition`` is merged)."""
    declared = copy.deepcopy(ECHO_DECLARATION)
    target = declared["implementations"][0]
    target["definition"].update(implementation.pop("definition", {}))
    target.update(implementation)
    return declared


async def test_a_declaration_is_implemented(agents, graphql) -> None:  # noqa: ANN001
    agent = await agents()
    held = f"declared-{uuid.uuid4().hex}"

    init = await agent.register("conf_21", session_id=session_id(), hash=held, **ECHO_DECLARATION)

    assert init["hash"] == held
    task = await graphql("conf_21").assign(init["agent"], "echo", {"x": 1})
    assert (await agent.receive_type("ASSIGN"))["task"] == task


async def test_the_held_hash_is_not_reconciled_again(agents) -> None:  # noqa: ANN001
    held = f"declared-{uuid.uuid4().hex}"
    first = await agents()
    await first.register("conf_22", session_id=session_id(), hash=held, **ECHO_DECLARATION)
    await first.close()

    # Refusable, but under the hash the agent holds: nothing is reconciled, so nothing refuses.
    again = await agents()
    init = await again.register(
        "conf_22", session_id=session_id(), hash=held, **declaration(effects="IRREVERSIBLE", definition={"pure": True})
    )
    assert init["hash"] == held


async def test_a_refused_declaration_is_answered_then_closed(agents) -> None:  # noqa: ANN001
    agent = await agents()

    await agent.send(
        {
            "type": "REGISTER",
            "token": "conf_23",
            "session_id": session_id(),
            "hash": f"refused-{uuid.uuid4().hex}",
            **declaration(effects="IRREVERSIBLE", definition={"pure": True}),
        }
    )

    error = await agent.receive()
    assert error["type"] == "PROTOCOL_ERROR"
    assert error["error"].startswith("Registration refused: Action echo is declared pure but its implementation's effects are IRREVERSIBLE")
    assert await agent.expect_close() == REGISTRATION_REJECTED


async def test_diagnostics_ride_on_init(agents) -> None:  # noqa: ANN001
    agent = await agents()

    init = await agent.register(
        "conf_24", session_id=session_id(), hash=f"diagnosed-{uuid.uuid4().hex}", **declaration(definition={"catalogs": ["not-registered"]})
    )

    assert [(d["code"], d["path"]) for d in init["diagnostics"]] == [("unknown_catalog", "Definition echo")]


async def test_without_a_hash_the_server_mints_one(agents) -> None:  # noqa: ANN001
    agent = await agents()

    init = await agent.register("conf_25", session_id=session_id(), **ECHO_DECLARATION)

    assert init["hash"], "a declaration without a hash is given one"


async def test_a_declaration_the_schema_refuses_is_answered_then_closed(agents) -> None:  # noqa: ANN001
    agent = await agents()
    broken = declaration()
    del broken["implementations"][0]["interface"]

    await agent.send({"type": "REGISTER", "token": "conf_26", "session_id": session_id(), "hash": "x", **broken})

    error = await agent.receive()
    assert error["type"] == "PROTOCOL_ERROR" and error["error"]
    assert await agent.expect_close() == SCHEMA
