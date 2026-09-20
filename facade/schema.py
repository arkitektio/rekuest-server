import strawberry
import strawberry_django
from facade import models, mutations, queries, subscriptions, types
from kante.types import Info
from rekuest_core.constants import interface_types, input_union_types
from kante.unions import unionElementOf
from strawberry_django.optimizer import DjangoOptimizerExtension
from authentikate.strawberry import AuthentikateExtension, AuthExtension, AuthSubscribeExtension
from typing import cast
from datalayer import mutations as datalayer_mutations
from datalayer.scalars import scalar_map as dscalar_map
from rekuest_core.scalars import scalar_map as rscalar_map
from facade.scalars import scalar_map as fscalar_map
import kante
from facade.types.base import scoped_get
from strawberry.schema.config import StrawberryConfig


def field(**kwargs):
    """A wrapper for field that attaches the auth extension."""
    return strawberry_django.field(extensions=[AuthExtension()], **kwargs)


def mutation(**kwargs):
    """A wrapper for mutation that attaches the auth extension."""
    return strawberry_django.mutation(extensions=[AuthExtension()], **kwargs)


def subscription(**kwargs) -> strawberry.subscription:
    """A wrapper for subscription that attaches the auth-subscribe extension."""
    return strawberry.subscription(extensions=[AuthSubscribeExtension()], **kwargs)


@strawberry.type(description="Root query type for fetching entities in the system.")
class Query:
    clients: list[types.Client] = field(description="List all registered clients.")
    hardware_records: list[types.HardwareRecord] = field(description="List of all hardware records.")
    agents: list[types.Agent] = field(description="Retrieve all compute agents.")
    actions: list[types.Action] = field(description="List of all available actions.")
    protocols: list[types.Protocol] = field(description="Retrieve protocols grouping actions.")
    implementations: list[types.Implementation] = field(description="All registered implementations.")
    test_results: list[types.TestResult] = field(description="Test results associated with test cases.")
    test_cases: list[types.TestCase] = field(description="All test cases.")
    shortcuts: list[types.Shortcut] = field(description="List of shortcuts.")
    toolboxes: list[types.Toolbox] = field(description="List of toolboxes containing shortcuts.")
    action = field(resolver=queries.action, description="Fetch a specific action.")
    similar_actions = field(resolver=queries.similar_actions, description="Actions whose name and description mean roughly what this action's do, nearest first: the org's other actions ranked by cosine distance between their embeddings. `filters` narrows the candidates like `actions` does; `maxDistance` (0 identical, 1 unrelated) cuts the tail, otherwise the nearest `limit` come back. Empty while the action has no vector yet or embeddings are off.")
    my_tasks = field(resolver=queries.my_tasks, description="Fetch the root tasks this client created (caller-scoped).")
    probe = field(resolver=queries.probe, description="Fetch a live (or lingering) probe by ID. Expired probes are gone — probes are never persisted.")
    probe_stats = field(resolver=queries.probe_stats, description="Live probe counts: instance-wide total plus your in-flight count and cap.")
    reusable_task_for = field(resolver=queries.reusable_task_for, description="The latest completed run of a PURE action with these exact args, or null — the replay primitive. Reuse decisions belong to the orchestrator.")
    event = field(resolver=queries.event, description="Fetch a specific event.")
    implementation_at = field(resolver=queries.implementation_at, description="Find implementation at given interface.")
    my_implementation_at = field(resolver=queries.my_implementation_at, description="Find your implementation at a specific interface.")
    checkout = field(resolver=queries.checkout, description="Materialize the latest state for a specific agent")
    checkout_agent = field(resolver=queries.checkout_agent, description="Materialize the latest states for a specific agent")
    dashboards: list[types.Dashboard] = field(description="All dashboards.")
    states: list[types.State] = field(description="All states from agents.")
    bloks: list[types.Blok] = field(description="List of UI Blok.")
    resolutions: list[types.Resolution] = field(description="All resolutions.")
    materialized_bloks: list[types.MaterializedBlok] = field(description="List of UI Blok.")
    ui_catalogs: list[types.UICatalog] = field(description="UI catalogs registered in the caller's organization: the components and operations UI apps can render and evaluate.")
    base_catalog = field(resolver=types.dashboard.base_catalog, description="The built-in base catalog every definition and blok is validated against before any registered UI catalog (virtual: shipped with the server, not registered).")
    state_definitions: list[types.StateDefinition] = field(description="Available state schemas.")
    memory_shelves: list[types.MemoryShelve] = field(description="All memory shelves.")
    memory_drawers: list[types.MemoryDrawer] = field(description="All memory drawers.")
    structures = field(resolver=types.structure.list_structures, description="All structures referenced by the org's action ports (derived, not registered).")
    structure_packages = field(resolver=types.structure.list_structure_packages, description="All structure packages referenced by the org's action ports (derived, not registered).")
    interfaces = field(resolver=types.structure.list_interfaces, description="All interfaces referenced by the org's action ports (derived, not registered).")
    tasks: list[types.Task] = field(description="All tasks.")
    resolved_implementations = field(resolver=queries.resolved_implementations, description="Fetch resolved dependencies for a resolution.")

    agent: types.Agent = field(resolver=queries.agent, description="Fetch a specific agent by ID or by app, version and device_id.")

    spaces: list[types.Space] = field(description="List all spaces.")
    space: types.Space = field(description="Fetch a specific space by ID.")
    placements: list[types.Placement] = field(description="List all placements.")
    placement: types.Placement = field(description="Fetch a specific placement by ID.")
    threed_models: list[types.ThreeDModel] = field(description="List all 3D models.")
    threed_model: types.ThreeDModel = field(description="Fetch a specific 3D model by ID.")

    sessions: list[types.Session] = field(description="List all sessions.")
    session: types.Session = field(description="Fetch a specific session by ID.")

    # Stats
    actionStats: types.ActionStats = field(resolver=types.ActionStatsResolver, description="Statistics about actions and their implementations.")
    taskStats: types.TaskStats = field(resolver=types.TaskStatsResolver, description="Statistics about tasks and their states.")

    state_for = field(resolver=queries.state_for, description="Retrieve state for a specific context.")

    task_boundaries: types.TaskBoundary | None = field(resolver=queries.task_boundaries, description="Get task boundaries.")
    session_boundaries: types.SessionBoundary | None = field(resolver=queries.session_boundaries, description="Get session boundaries.")
    state_at_global_rev: list[types.Snapshot] = field(resolver=queries.state_at_global_rev, description="Get state at global revision.")
    forward_events_after_rev: list[types.Patch] = field(resolver=queries.forward_events_after_rev, description="Get forward events after revision.")
    patch_events_between_global_revs: list[types.Patch] = field(resolver=queries.patch_events_between_global_revs, description="Get patch events between global revisions.")
    snapshots_around_rev: list[types.Snapshot] = field(resolver=queries.snapshots_around_rev, description="Get snapshots around revision.")

    @field(description="Fetch a client by ID.")
    def resolution(self, info: Info, id: strawberry.ID) -> types.Resolution:
        return cast(types.Resolution, scoped_get(models.Resolution, info, id, field="organization"))

    @field(description="Get a specific state by ID.")
    def state(self, info: Info, id: strawberry.ID) -> types.State:
        return cast(types.State, scoped_get(models.State, info, id, field="agent__organization"))

    structure_package = field(resolver=types.structure.get_structure_package, description="Fetch a structure package by its key (derived from port identifiers).")
    interface = field(resolver=types.structure.get_interface, description="Fetch an interface by its '@package/key' identifier (derived from port identifiers).")
    structure = field(resolver=types.structure.get_structure, description="Fetch a structure by its '@package/key' identifier (derived from port identifiers).")

    @field(description="Fetch a memory shelve by ID.")
    def memory_shelve(self, info: Info, id: strawberry.ID) -> types.MemoryShelve:
        return cast(types.MemoryShelve, scoped_get(models.MemoryShelve, info, id, field="organization"))

    @field(description="Fetch a memory drawer by ID.")
    def memory_drawer(self, info: Info, id: strawberry.ID) -> types.MemoryDrawer:
        return cast(types.MemoryDrawer, scoped_get(models.MemoryDrawer, info, id, field="shelve__organization"))

    @field(description="Get a blok by ID.")
    def blok(self, info: Info, id: strawberry.ID) -> types.Blok:
        return cast(types.Blok, scoped_get(models.Blok, info, id, field="organization"))

    @field(description="Get a materialized blok by ID.")
    def materialized_blok(self, info: Info, id: strawberry.ID) -> types.MaterializedBlok:
        return cast(types.MaterializedBlok, scoped_get(models.MaterializedBlok, info, id, field="blok__organization"))

    @field(description="Get a UI catalog by ID.")
    def ui_catalog(self, info: Info, id: strawberry.ID) -> types.UICatalog:
        return cast(types.UICatalog, scoped_get(models.UICatalog, info, id, field="organization"))

    @field(description="Retrieve a state definition by ID.")
    def state_definition(self, info: Info, id: strawberry.ID) -> types.StateDefinition:
        return cast(types.StateDefinition, scoped_get(models.StateDefinition, info, id, field="organization"))

    @field(description="Get toolbox by ID.")
    def toolbox(self, info: Info, id: strawberry.ID) -> types.Toolbox:
        return cast(types.Toolbox, scoped_get(models.Toolbox, info, id, field="organization"))

    @field(description="Retrieve shortcut by ID.")
    def shortcut(self, info: Info, id: strawberry.ID) -> types.Shortcut:
        return cast(types.Shortcut, scoped_get(models.Shortcut, info, id, field="toolbox__organization"))

    @field(description="Get hardware record by ID.")
    def hardware_record(self, info: Info, id: strawberry.ID) -> types.HardwareRecord:
        return cast(types.HardwareRecord, scoped_get(models.HardwareRecord, info, id, field="agent__organization"))

    @field(description="Get dashboard by ID.")
    def dashboard(self, info: Info, id: strawberry.ID) -> types.Dashboard:
        return cast(types.Dashboard, scoped_get(models.Dashboard, info, id, field="organization"))

    @field(description="Fetch a dependency by ID.")
    def dependency(self, info: Info, id: strawberry.ID) -> types.Dependency:
        return cast(types.Dependency, scoped_get(models.Dependency, info, id, field="implementation__action__organization"))

    @field(description="Retrieve test case by ID.")
    def test_case(self, info: Info, id: strawberry.ID) -> types.TestCase:
        return cast(types.TestCase, scoped_get(models.TestCase, info, id, field="action__organization"))

    @field(description="Get test result by ID.")
    def test_result(self, info: Info, id: strawberry.ID) -> types.TestResult:
        return cast(types.TestResult, scoped_get(models.TestResult, info, id, field="case__action__organization"))

    @field(description="Get implementation by ID.")
    def implementation(self, info: Info, id: strawberry.ID) -> types.Implementation:
        return cast(types.Implementation, scoped_get(models.Implementation, info, id, field="action__organization"))

    @field(description="Fetch task by ID.")
    def task(self, info: Info, id: strawberry.ID) -> types.Task:
        return cast(types.Task, scoped_get(models.Task, info, id, field="agent__organization"))


@strawberry.type(description="Root mutation type for executing write operations on the API.")
class Mutation:
    create_implementation = mutation(resolver=mutations.create_implementation, description="Create a new implementation entry.")
    ack = mutation(resolver=mutations.ack, description="Acknowledge a task.")
    bounce = mutation(resolver=mutations.bounce, description="Bounce an agent so it reconnects.")
    kick = mutation(resolver=mutations.kick, description="Kick an agent to force disconnect. It will fail and not reconnect.")
    assign = mutation(resolver=mutations.assign, description="Assign a task to an agent.")
    probe = mutation(resolver=mutations.probe, description="Fire a probe at an agent — zero persistence, redis-held state under a TTL, never appears in task history. For high-frequency interactive work (previews, live parameter tweaks).")
    cancel_probe = mutation(resolver=mutations.cancel_probe, description="Cancel a probe. Idempotent: cancelling a finished probe is a no-op returning its terminal state.")
    pause_probe = mutation(resolver=mutations.pause_probe, description="Pause a probe. Idempotent on finished probes; the agent's Paused report settles the state.")
    resume_probe = mutation(resolver=mutations.resume_probe, description="Resume a paused probe. Idempotent on finished probes; the agent's Resumed report settles the state.")
    cancel = mutation(resolver=mutations.cancel, description="Cancel an active task.")
    pause = mutation(resolver=mutations.pause, description="Pause an ongoing task.")
    resume = mutation(resolver=mutations.resume, description="Resume a paused task.")
    collect = mutation(resolver=mutations.collect, description="Collect results from a task.")
    interrupt = mutation(resolver=mutations.interrupt, description="Interrupt the execution of a task.")
    block = mutation(resolver=mutations.block, description="Block an agent from connecting.")
    unblock = mutation(resolver=mutations.unblock, description="Unblock a previously blocked agent.")
    delete_implementation = mutation(resolver=mutations.delete_implementation, description="Delete a registered implementation.")
    set_higher_order = mutation(resolver=mutations.set_higher_order, description="Mark an implementation as a higher-order wrapper of a lower implementation, with a projection config.")
    ensure_agent = mutation(resolver=mutations.ensure_agent, description="Ensure agent record exists or is up to date.")
    create_test_case = mutation(resolver=mutations.create_test_case, description="Create a new test case.")
    create_test_result = mutation(resolver=mutations.create_test_result, description="Create a test result record.")
    shelve_in_memory_drawer = mutation(resolver=mutations.shelve_in_memory_drawer, description="Shelve data into a memory drawer.")
    unshelve_memory_drawer = mutation(resolver=mutations.unshelve_memory_drawer, description="Unshelve data from a memory drawer.")
    create_dashboard = mutation(resolver=mutations.create_dashboard, description="Create a dashboard layout.")
    create_blok = mutation(resolver=mutations.create_blok, description="Create a user interface panel.")
    materialize_blok = mutation(resolver=mutations.materialize_blok, description="Materialize a UI blok into a concrete instance on a dashboard.")
    pin_agent = mutation(resolver=mutations.pin_agent, description="Pin an agent to the user.")
    pin_implementation = mutation(resolver=mutations.pin_implementation, description="Pin an implementation to the user.")
    delete_agent = mutation(resolver=mutations.delete_agent, description="Delete an agent record.")
    update_agent = mutation(resolver=mutations.update_agent, description="Update properties of an agent such as its name.")
    create_shortcut = mutation(resolver=mutations.create_shortcut, description="Create a shortcut to an action.")
    delete_shortcut = mutation(resolver=mutations.delete_shortcut, description="Delete a shortcut.")
    create_toolbox = mutation(resolver=mutations.create_toolbox, description="Create a new toolbox with shortcuts.")
    delete_toolbox = mutation(resolver=mutations.delete_toolbox, description="Delete a toolbox by ID.")
    cleanup_actions = mutation(resolver=mutations.cleanup_actions, description="Delete unreferenced actions from the system.")
    auto_resolve = mutation(resolver=mutations.auto_resolve, description="Automatically resolve dependencies for an implementation.")
    create_resolution = mutation(resolver=mutations.create_resolution, description="Create a resolution for an implementation.")
    update_resolution = mutation(resolver=mutations.update_resolution, description="Update an existing resolution.")
    delete_resolution = mutation(resolver=mutations.delete_resolution, description="Delete a resolution by ID.")

    # Datalayer. These use the local ``mutation()`` wrapper, not ``kante.django_mutation``:
    # the latter is plain ``strawberry_django.mutation`` and so skips ``AuthExtension``, which
    # left these three as the only mutations in the schema carrying no @auth directive.
    request_media_upload = mutation(
        description="Upload media and return a URL for access",
        resolver=datalayer_mutations.request_media_upload,
    )
    finish_media_upload = mutation(
        description="Finalize a media upload after the client has written the object",
        resolver=datalayer_mutations.finish_media_upload,
    )
    request_media_access = mutation(
        description="Request temporary S3 read credentials for a media file",
        resolver=datalayer_mutations.request_media_access,
    )

    # Dashboard
    delete_dashboard = mutation(resolver=mutations.delete_dashboard, description="Delete a dashboard by ID.")
    update_dashboard = mutation(resolver=mutations.update_dashboard, description="Update properties of a dashboard such as its name, associated bloks, or organization.")

    # Blok
    delete_blok = mutation(resolver=mutations.delete_blok, description="Delete a blok by ID.")
    update_blok = mutation(resolver=mutations.update_blok, description="Update properties of a blok such as its name, description, components, demo state, catalog, or dependencies.")
    delete_materialized_blok = mutation(resolver=mutations.delete_materialized_blok, description="Delete a materialized blok by ID.")
    update_materialized_blok = mutation(resolver=mutations.update_materialized_blok, description="Update properties of a materialized blok such as its agent mappings.")
    register_ui_catalog = mutation(resolver=mutations.register_ui_catalog, description="Register the components and operations a UI app can render and evaluate (upsert by name in the caller's organization). Bloks and definitions that name the catalog are validated against it.")
    # Implement Agent
    implement_agent = mutation(resolver=mutations.implement_agent, description="Implement an agent with given states and implementations. This is used to set up an agent with its initial configuration and capabilities.")

    # 3D Model
    create_threed_model = mutation(resolver=mutations.create_threed_model, description="Create a new 3D model.")
    update_threed_model = mutation(resolver=mutations.update_threed_model, description="Update an existing 3D model.")
    delete_threed_model = mutation(resolver=mutations.delete_threed_model, description="Delete a 3D model.")

    # Space

    create_space = mutation(resolver=mutations.create_space, description="Create a new space.")
    update_space = mutation(resolver=mutations.update_space, description="Update an existing space.")
    delete_space = mutation(resolver=mutations.delete_space, description="Delete a space.")

    create_placement = mutation(resolver=mutations.create_placement, description="Create a new placement for an agent in a space.")
    update_placement = mutation(resolver=mutations.update_placement, description="Update an existing placement.")
    delete_placement = mutation(resolver=mutations.delete_placement, description="Delete a placement.")


@strawberry.type(description="Root subscription type for real-time event streams from the system.")
class Subscription:
    new_actions = subscription(resolver=subscriptions.new_actions, description="Subscribe to notifications when new actions are created.")
    mytasks = subscription(resolver=subscriptions.mytasks, description="Subscribe to root tasks created by this client (caller-scoped).")
    tasks = subscription(resolver=subscriptions.tasks, description="Subscribe to root task changes across the whole organization.")
    agents = subscription(resolver=subscriptions.agents, description="Subscribe to updates on agent connections and statuses.")
    implementation_change = subscription(resolver=subscriptions.implementation_change, description="Subscribe to changes in implementations.")
    implementations = subscription(resolver=subscriptions.implementations, description="Subscribe to creation or updates of implementations.")
    state_update_events = subscription(resolver=subscriptions.state_update_events, description="Subscribe to updates of state values and patches.")
    latest_patches = subscription(resolver=subscriptions.latest_patches, description="Subscribe to latest patches for specific agents or states.")
    watch_state = subscription(resolver=subscriptions.watch_state, description="Watch a state: yields the current snapshot then streams patches.")
    watch_agent = subscription(resolver=subscriptions.watch_agent, description="Watch an agent: yields snapshots for all states then streams patches.")
    child_tasks = subscription(resolver=subscriptions.child_tasks, description="Subscribe to all descendant task changes of a task.")
    agent_tasks = subscription(resolver=subscriptions.agent_tasks, description="Subscribe to task create/update for a specific agent.")
    probe_events = subscription(resolver=subscriptions.probe_events, description="Stream the events of one probe (caller-scoped, payload-carrying). Emits a state snapshot first when events already happened.")


schema = kante.Schema(
    query=Query,
    subscription=Subscription,
    mutation=Mutation,
    extensions=[
        DjangoOptimizerExtension,
        AuthentikateExtension,
    ],
    schema_directives=[unionElementOf],
    types=interface_types + input_union_types,
    config=StrawberryConfig(
        scalar_map={
            **dscalar_map,
            **rscalar_map,
            **fscalar_map,
        },
    ),
    # We really need to register
    # all the types here, otherwise the schema will not be able to resolve them
    # and will throw a cryptic error
)
