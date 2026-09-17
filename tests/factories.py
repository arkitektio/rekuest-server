"""Shared builders for the test suite.

Two groups live here:

* **Sync model builders** (``create_registry_bundle``, ``create_agent_for_registry``,
  ``create_action_for_organization``) used by the model tests.
* **Agent object-graph builders** used by the websocket tests — the async
  ``seed_agent`` (pre-creates the agent the consumer looks up for a token) plus the
  ``build_*`` helpers that assemble Action -> Implementation -> Task / State
  graphs. The ``build_*`` names are ``sync_to_async``-wrapped for use from async tests.
"""

from asgiref.sync import sync_to_async

from authentikate.expand import (
    aexpand_client_from_token,
    aexpand_organization_from_token,
    aexpand_user_from_token,
)
from authentikate.models import App, Client, Device, Organization, Release, User
from authentikate.utils import authenticate_token_or_none

from facade import enums
from facade.models import (
    Action,
    Agent,
    Task,
    TaskEvent,
    Caller,
    Implementation,
    State,
    StateDefinition,
)

TEST_TOKEN = "test"


# --------------------------------------------------------------------------- #
# Sync model builders (used by the model tests)
# --------------------------------------------------------------------------- #
def create_registry_bundle(prefix: str) -> tuple[User, Client, Organization, Caller]:
    user = User.objects.create(username=f"{prefix}-user", password="testpass")
    client = Client.objects.create(client_id=f"{prefix}-client")
    org = Organization.objects.create(slug=f"{prefix}-org")
    caller = Caller.objects.create(client=client, user=user, organization=org)
    return user, client, org, caller


def create_agent_for_registry(registry: Caller, user: User, organization: Organization, prefix: str, **overrides) -> Agent:
    release = Release.objects.create(app=App.objects.create(identifier=f"{prefix}-app"), version="1.0.0")
    device = Device.objects.create(device_id=f"{prefix}-device")
    registry.client.device = device
    registry.client.save()

    agent_data = {
        "app": release.app,
        "hash": f"{prefix}-hash",
        "release": release,
        "user": user,
        "client": registry.client,
        "organization": organization,
    }
    agent_data.update(overrides)

    return Agent.objects.create(**agent_data)


def create_action_for_organization(organization: Organization, prefix: str, **overrides) -> Action:
    app = App.objects.create(identifier=f"{prefix}-app")
    action_data = {
        "app": app,
        "key": f"{prefix}-key",
        "version": "1.0.0",
        "name": f"{prefix} action",
        "description": f"{prefix} description",
        "hash": f"{prefix}-hash",
        "organization": organization,
    }
    action_data.update(overrides)

    return Action.objects.create(**action_data)


# --------------------------------------------------------------------------- #
# Agent seeding (async) — reproduces ``ensureAgent`` for the websocket tests
# --------------------------------------------------------------------------- #
async def seed_agent(instance_id, token=TEST_TOKEN, blocked=False):
    """Pre-create the agent the consumer will look up for ``token``.

    Uses the same authentikate expansion the consumer uses, so the derived
    (client, user, organization) matches and ``on_register`` finds (rather than creates) the agent.
    ``instance_id`` is only a label used to make seeded rows distinguishable.
    """
    decoded = await authenticate_token_or_none(token)
    user = await aexpand_user_from_token(decoded)
    client = await aexpand_client_from_token(decoded)
    organization = await aexpand_organization_from_token(decoded)

    app, _ = await App.objects.aget_or_create(identifier="ws-test-app")
    release, _ = await Release.objects.aget_or_create(app=app, version="1.0.0")
    device, _ = await Device.objects.aget_or_create(device_id="ws-test-device")
    client.device = device
    await client.asave()

    agent, _ = await Agent.objects.aupdate_or_create(
        client=client,
        user=user,
        organization=organization,
        defaults=dict(
            app=app,
            release=release,
            hash=f"{instance_id}-hash",
            blocked=blocked,
        ),
    )
    return agent


# --------------------------------------------------------------------------- #
# Object-graph builders (run synchronously, wrapped via sync_to_async)
# --------------------------------------------------------------------------- #
def _build_task(prefix, *, effect="NONE", idempotent=False, pure=False, parent=None, agent_pk=None):
    """Create a standalone Action -> Implementation -> Task graph.

    ``effect`` sets the implementation's effect class; ``parent`` wires the tree shape.

    ``agent_pk`` binds the task to an existing agent instead of minting a fresh one. Pass it
    whenever the test streams events over an agent socket: the persist backend now resolves
    reported tasks with an ``agent_id`` predicate, so an agent may only report on its *own*
    work. Omitting it yields a task owned by a throwaway agent, which is right for tests that
    never report over a socket.
    """
    user = User.objects.create(username=f"{prefix}-user", password="x", sub=f"{prefix}-sub")
    device = Device.objects.create(device_id=f"{prefix}-device")
    client = Client.objects.create(client_id=f"{prefix}-client", device=device)
    org = Organization.objects.create(slug=f"{prefix}-org")
    caller = Caller.objects.create(client=client, user=user, organization=org)

    app = App.objects.create(identifier=f"{prefix}-app")
    release = Release.objects.create(app=app, version="1.0.0")
    agent = (
        Agent.objects.get(pk=agent_pk)
        if agent_pk is not None
        else Agent.objects.create(
            app=app,
            hash=f"{prefix}-hash",
            release=release,
            user=user,
            client=client,
            organization=org,
        )
    )

    action = Action.objects.create(
        app=app,
        key=f"{prefix}-key",
        version="1.0.0",
        name=f"{prefix} action",
        description=f"{prefix} description",
        hash=f"{prefix}-action-hash",
        organization=org,
        idempotent=idempotent,
        pure=pure,
    )
    implementation = Implementation.objects.create(
        release=release,
        interface=f"{prefix}-iface",
        action=action,
        agent=agent,
        effect=effect,
    )

    return Task.objects.create(
        caller=caller,
        action=action,
        agent=agent,
        implementation=implementation,
        parent=parent,
        latest_event_kind=enums.TaskEventKind.STARTED,
        latest_instruct_kind=enums.TaskInstructChoices.ASSIGN,
    )


def _build_unimplemented_task_for_agent(agent_pk, prefix):
    """An unfinished task owned directly by ``agent`` with NO implementation.

    Exercises the disconnect path: such rows are found by the direct ``agent`` FK
    but would be missed by an ``implementation__agent`` filter (implementation is
    null). Guards the B1 fix in ``ModelPersistBackend.on_agent_disconnected``.
    """
    agent = Agent.objects.get(pk=agent_pk)
    app = App.objects.create(identifier=f"{prefix}-app")
    action = Action.objects.create(
        app=app,
        key=f"{prefix}-key",
        version="1.0.0",
        name=f"{prefix} action",
        description=f"{prefix} description",
        hash=f"{prefix}-action-hash",
        organization=agent.organization,
    )
    return Task.objects.create(
        caller=None,
        action=action,
        agent=agent,
        implementation=None,
        latest_event_kind=enums.TaskEventKind.STARTED,
        latest_instruct_kind=enums.TaskInstructChoices.ASSIGN,
    )


def _build_task_for_agent_caller(agent_pk, prefix, parent=None, root=None):
    """A task whose ``caller`` is the agent's own identity.

    Used by the caller-event return tests: because the task's caller matches the
    registered agent's caller (group ``task_caller_{caller_id}``), events on it are streamed
    back to that agent's socket as ``Caller*`` messages.

    ``parent``/``root`` wire the task into a tree — pass ``parent=root_task, root=root_task``
    for a direct child, or ``parent=child, root=root_task`` for a deeper descendant. Leaving
    both ``None`` builds a root task (``root_id IS NULL``).
    """
    agent = Agent.objects.select_related("user", "client", "organization").get(pk=agent_pk)
    caller, _ = Caller.objects.get_or_create(client=agent.client, user=agent.user, organization=agent.organization)

    app = App.objects.create(identifier=f"{prefix}-app")
    release = Release.objects.create(app=app, version="1.0.0")
    action = Action.objects.create(
        app=app,
        key=f"{prefix}-key",
        version="1.0.0",
        name=f"{prefix} action",
        description=f"{prefix} description",
        hash=f"{prefix}-action-hash",
        organization=agent.organization,
    )
    implementation = Implementation.objects.create(
        release=release,
        interface=f"{prefix}-iface",
        action=action,
        agent=agent,
    )
    return Task.objects.create(
        caller=caller,
        action=action,
        agent=agent,
        implementation=implementation,
        parent=parent,
        root=root,
        latest_event_kind=enums.TaskEventKind.STARTED,
        latest_instruct_kind=enums.TaskInstructChoices.ASSIGN,
    )


def _touch_agent(agent_pk):
    """Re-save an agent (an ``update`` fan-out on the ``agents`` feed)."""
    agent = Agent.objects.get(pk=agent_pk)
    agent.save()
    return agent


def _build_task_event(task_pk, kind=enums.TaskEventChoices.COMPLETED, message=None, progress=None, returns=None):
    """Persist a ``TaskEvent`` on a task, firing the same ``post_save`` fan-out as real dispatch.

    On a root task this drives a ``mytasks`` / ``tasks`` ``event`` over the change feeds.
    """
    task = Task.objects.get(pk=task_pk)
    return TaskEvent.objects.create(
        task=task,
        kind=kind,
        message=message,
        progress=progress,
        returns=returns,
    )


def _build_implementation_for_agent(agent_pk, prefix, needs_token=True, allow_probe=True):
    """An Action + Implementation owned by ``agent`` (connectable assign target).

    ``allow_probe`` defaults True so the call tests can target it; the flag only
    gates the ``call`` mutation and is invisible to the task paths.
    """
    agent = Agent.objects.select_related("app", "release", "organization").get(pk=agent_pk)
    action = Action.objects.create(
        app=agent.app,
        key=f"{prefix}-key",
        version="1.0.0",
        name=f"{prefix} action",
        description=f"{prefix} description",
        hash=f"{prefix}-action-hash",
        organization=agent.organization,
        allow_probe=allow_probe,
    )
    return Implementation.objects.create(
        release=agent.release,
        interface=f"{prefix}-iface",
        action=action,
        agent=agent,
        needs_token=needs_token,
    )


def _seed_throwaway_agent_graph(prefix):
    """A standalone Agent with its own user/client/org/app — no token, no socket.

    For tests that only need *an* agent to hang rows off (locks, sessions, bloks), where
    ``seed_agent`` would tie them to the shared static-token identity and to each other.
    """
    user = User.objects.create(username=f"{prefix}-user", password="x", sub=f"{prefix}-sub")
    device = Device.objects.create(device_id=f"{prefix}-device")
    client = Client.objects.create(client_id=f"{prefix}-client", device=device)
    organization = Organization.objects.create(slug=f"{prefix}-org")
    app = App.objects.create(identifier=f"{prefix}-app")
    release = Release.objects.create(app=app, version="1.0.0")
    return Agent.objects.create(app=app, hash=f"{prefix}-hash", release=release, user=user, client=client, organization=organization)


def _build_state_for_agent(agent_pk, interface, prefix):
    """Create a State (and its definition) attached to an existing agent."""
    agent = Agent.objects.get(pk=agent_pk)
    definition = StateDefinition.objects.create(
        name=f"{prefix} state",
        hash=f"{prefix}-state-hash",
        ports=[],
        description=f"{prefix} state def",
        organization=agent.organization,
    )
    return State.objects.create(definition=definition, interface=interface, agent_id=agent_pk, value={})


def _build_webhook_agent(prefix, secret="s3cr3t", hook_url="https://hook.example/in"):
    """A HookAgent (kind=WEBHOOK) with a hook_url + shared secret, plus its identity graph."""
    user = User.objects.create(username=f"{prefix}-user", password="x", sub=f"{prefix}-sub")
    device = Device.objects.create(device_id=f"{prefix}-device")
    client = Client.objects.create(client_id=f"{prefix}-client", device=device)
    org = Organization.objects.create(slug=f"{prefix}-org")
    app = App.objects.create(identifier=f"{prefix}-app")
    release = Release.objects.create(app=app, version="1.0.0")
    return Agent.objects.create(
        app=app,
        hash=f"{prefix}-hash",
        release=release,
        user=user,
        client=client,
        organization=org,
        kind=enums.AgentKind.WEBHOOK.value,
        hook_url=hook_url,
        hook_url_secret=secret,
    )


build_task = sync_to_async(_build_task)
build_task_for_agent_caller = sync_to_async(_build_task_for_agent_caller)
build_task_event = sync_to_async(_build_task_event)
touch_agent = sync_to_async(_touch_agent)
build_implementation_for_agent = sync_to_async(_build_implementation_for_agent)
build_webhook_agent = sync_to_async(_build_webhook_agent)
build_unimplemented_task_for_agent = sync_to_async(_build_unimplemented_task_for_agent)
build_state_for_agent = sync_to_async(_build_state_for_agent)
seed_throwaway_agent_graph = sync_to_async(_seed_throwaway_agent_graph)
