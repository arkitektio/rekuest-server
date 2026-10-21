"""A custom blok's history: every write is a revision, an edit names the one it started from.

An assistant designs a blok over a long turn: it reads the head, works, and writes. ``editBlok``
writes the content and this materialization's bindings at once, refuses a base that is not the
head any more, and with ``dryRun`` keeps nothing. ``revertBlok`` is an undo that grows history;
a pinned materialization stays where it is; one that follows the head says which required
dependencies an edit elsewhere left unbound on it.
"""

import pytest
from asgiref.sync import sync_to_async
from authentikate.models import App, Client, Device, Organization, Release, User
from kante.context import HttpContext

from facade.models import Agent, Blok, BlokAgentMapping, BlokRevision, Caller, MaterializedBlok, UICatalog
from facade.schema import schema
from tests.factories import _build_task
from tests.graphql_ops import CREATE_BLOK, MATERIALIZE_BLOK, UPDATE_BLOK

BOX = [{"id": "root", "component": "Box"}]
TEXT = [{"id": "root", "component": "Text"}]

REVISION = """
    id
    number
    parent { id }
    message
    task { id }
    componentTree
    dependencies { key optional }
"""

BLOK = f"""
    query Blok($id: ID!) {{
        blok(id: $id) {{
            componentTree
            head {{ {REVISION} }}
            revisions {{ number message }}
            dependencies {{ key }}
        }}
    }}
"""

MATERIALIZED = f"""
    query Materialized($id: ID!) {{
        materializedBlok(id: $id) {{
            blok {{ id name provided }}
            pinnedRevision {{ id }}
            revision {{ {REVISION} }}
            unboundDependencies
            agentMappings {{ key agent {{ id }} }}
        }}
    }}
"""

EDIT_BLOK = f"""
    mutation EditBlok($input: EditBlokInput!) {{
        editBlok(input: $input) {{
            applied
            forked
            blok {{ id name provided }}
            revision {{ {REVISION} }}
            materializedBlok {{ id blok {{ id provided }} agentMappings {{ key agent {{ id }} }} }}
            diagnostics {{ message }}
        }}
    }}
"""

REVERT_BLOK = f"""
    mutation RevertBlok($input: RevertBlokInput!) {{
        revertBlok(input: $input) {{ {REVISION} }}
    }}
"""

PIN = """
    mutation Pin($input: PinMaterializedBlokInput!) {
        pinMaterializedBlok(input: $input) { id pinnedRevision { id } }
    }
"""


def _drawn(tree: list[dict]) -> list[dict]:
    """A tree without the unset parts a parsed component is stored with."""
    return [{key: value for key, value in node.items() if value is not None} for node in tree]


def _agent_in(org: Organization, user: User, prefix: str) -> Agent:
    client = Client.objects.create(client_id=f"{prefix}-client", device=Device.objects.create(device_id=f"{prefix}-device"))
    Caller.objects.create(client=client, user=user, organization=org)
    release = Release.objects.create(app=App.objects.create(identifier=f"{prefix}-app"), version="1.0.0")
    return Agent.objects.create(app=release.app, hash=f"{prefix}-hash", release=release, user=user, client=client, organization=org)


async def _run(context: HttpContext, operation: str, variables: dict) -> dict:
    result = await schema.execute(operation, context_value=context, variable_values=variables)
    assert not result.errors, result.errors
    assert result.data is not None
    return result.data


async def _refused(context: HttpContext, operation: str, variables: dict) -> str:
    result = await schema.execute(operation, context_value=context, variable_values=variables)
    assert result.errors, "expected the write to be refused"
    return result.errors[0].message


async def _blok_with_two_materializations(context: HttpContext) -> dict:
    """A custom blok (revision 1) shown by two materializations."""
    created = await _run(context, CREATE_BLOK, {"input": {"name": "panel", "components": BOX}})
    blok_id = created["createBlok"]["id"]
    first = await _run(context, MATERIALIZE_BLOK, {"input": {"blok": blok_id, "name": "first"}})
    second = await _run(context, MATERIALIZE_BLOK, {"input": {"blok": blok_id, "name": "second"}})
    head = (await _run(context, BLOK, {"id": blok_id}))["blok"]["head"]
    return {"blok": blok_id, "first": first["materializeBlok"]["id"], "second": second["materializeBlok"]["id"], "head": head["id"]}


@sync_to_async
def _revision_count(blok_id: str) -> int:
    return BlokRevision.objects.filter(blok_id=blok_id).count()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestBlokRevisions:
    async def test_every_write_is_a_revision_and_one_that_changes_nothing_is_not(self, authenticated_context: HttpContext) -> None:
        created = await _run(authenticated_context, CREATE_BLOK, {"input": {"name": "history", "components": BOX}})
        blok_id = created["createBlok"]["id"]
        await _run(authenticated_context, UPDATE_BLOK, {"input": {"id": blok_id, "components": TEXT}})
        await _run(authenticated_context, UPDATE_BLOK, {"input": {"id": blok_id, "components": TEXT}})

        blok = (await _run(authenticated_context, BLOK, {"id": blok_id}))["blok"]
        assert [revision["number"] for revision in blok["revisions"]] == [2, 1]
        assert blok["head"]["number"] == 2
        assert blok["head"]["parent"] is not None
        assert blok["componentTree"] == blok["head"]["componentTree"]

    async def test_an_edit_lands_on_the_head_with_its_bindings(self, authenticated_context: HttpContext) -> None:
        env = await _blok_with_two_materializations(authenticated_context)
        org, user = authenticated_context.request.organization, authenticated_context.request.user
        stage = await sync_to_async(_agent_in)(org, user, "stage")

        edited = await _run(
            authenticated_context,
            EDIT_BLOK,
            {
                "input": {
                    "materializedBlok": env["first"],
                    "base": env["head"],
                    "components": TEXT,
                    "dependencies": [{"key": "stage"}],
                    "agentMappings": [{"key": "stage", "agent": str(stage.id)}],
                    "message": "Show the stage",
                }
            },
        )
        edit = edited["editBlok"]
        assert edit["applied"] and not edit["forked"]
        assert edit["revision"]["number"] == 2
        assert edit["revision"]["parent"] == {"id": env["head"]}
        assert edit["revision"]["message"] == "Show the stage"
        assert edit["revision"]["dependencies"] == [{"key": "stage", "optional": False}]
        assert edit["materializedBlok"]["agentMappings"] == [{"key": "stage", "agent": {"id": str(stage.id)}}]

        # The other materialization follows the head too, and says what it now lacks.
        other = (await _run(authenticated_context, MATERIALIZED, {"id": env["second"]}))["materializedBlok"]
        assert other["revision"]["number"] == 2
        assert other["unboundDependencies"] == ["stage"]
        first = (await _run(authenticated_context, MATERIALIZED, {"id": env["first"]}))["materializedBlok"]
        assert first["unboundDependencies"] == []

    async def test_an_edit_from_a_stale_base_is_refused(self, authenticated_context: HttpContext) -> None:
        env = await _blok_with_two_materializations(authenticated_context)
        # Someone else changes the blok meanwhile.
        await _run(authenticated_context, UPDATE_BLOK, {"input": {"id": env["blok"], "components": TEXT}})

        message = await _refused(authenticated_context, EDIT_BLOK, {"input": {"materializedBlok": env["first"], "base": env["head"], "demoState": {"x": 1}}})
        assert "changed since revision 1" in message
        assert await _revision_count(env["blok"]) == 2

    async def test_a_dry_run_checks_and_keeps_nothing(self, authenticated_context: HttpContext) -> None:
        env = await _blok_with_two_materializations(authenticated_context)

        checked = await _run(authenticated_context, EDIT_BLOK, {"input": {"materializedBlok": env["first"], "base": env["head"], "components": TEXT, "catalog": "brand-new", "dryRun": True}})
        assert checked["editBlok"]["applied"] is False
        assert checked["editBlok"]["revision"] is None
        assert await _revision_count(env["blok"]) == 1
        assert not await sync_to_async(UICatalog.objects.filter(name="brand-new").exists)()

        # A dry run refuses what the write would.
        message = await _refused(authenticated_context, EDIT_BLOK, {"input": {"materializedBlok": env["first"], "base": env["head"], "dependencies": [{"key": "stage"}], "dryRun": True}})
        assert "required but no agent was mapped" in message

    async def test_a_refused_binding_writes_nothing(self, authenticated_context: HttpContext) -> None:
        env = await _blok_with_two_materializations(authenticated_context)

        await _refused(
            authenticated_context,
            EDIT_BLOK,
            {"input": {"materializedBlok": env["first"], "base": env["head"], "components": TEXT, "dependencies": [{"key": "stage"}], "agentMappings": [{"key": "stage", "agent": "999999"}]}},
        )
        blok = (await _run(authenticated_context, BLOK, {"id": env["blok"]}))["blok"]
        assert blok["head"]["id"] == env["head"]
        assert blok["dependencies"] == []
        assert await sync_to_async(BlokAgentMapping.objects.count)() == 0

    async def test_revert_is_a_new_revision_with_the_old_content(self, authenticated_context: HttpContext) -> None:
        env = await _blok_with_two_materializations(authenticated_context)
        edited = await _run(authenticated_context, EDIT_BLOK, {"input": {"materializedBlok": env["first"], "base": env["head"], "components": TEXT}})
        second = edited["editBlok"]["revision"]["id"]

        # Reverting from a base that is not the head any more is refused like an edit.
        await _refused(authenticated_context, REVERT_BLOK, {"input": {"blok": env["blok"], "to": env["head"], "base": env["head"]}})

        reverted = (await _run(authenticated_context, REVERT_BLOK, {"input": {"blok": env["blok"], "to": env["head"], "base": second}}))["revertBlok"]
        assert reverted["number"] == 3
        assert reverted["message"] == "Back to revision 1"
        assert _drawn(reverted["componentTree"]) == BOX
        blok = (await _run(authenticated_context, BLOK, {"id": env["blok"]}))["blok"]
        assert [revision["number"] for revision in blok["revisions"]] == [3, 2, 1]
        assert _drawn(blok["componentTree"]) == BOX

    async def test_a_pinned_materialization_stays_until_edited_through(self, authenticated_context: HttpContext) -> None:
        env = await _blok_with_two_materializations(authenticated_context)
        await _run(authenticated_context, PIN, {"input": {"id": env["second"], "revision": env["head"]}})

        edited = await _run(authenticated_context, EDIT_BLOK, {"input": {"materializedBlok": env["first"], "base": env["head"], "components": TEXT}})
        head = edited["editBlok"]["revision"]["id"]
        pinned = (await _run(authenticated_context, MATERIALIZED, {"id": env["second"]}))["materializedBlok"]
        assert pinned["revision"]["number"] == 1
        assert _drawn(pinned["revision"]["componentTree"]) == BOX

        # Edited where it is shown, so shown as edited.
        await _run(authenticated_context, EDIT_BLOK, {"input": {"materializedBlok": env["second"], "base": head, "demoState": {"x": 1}}})
        unpinned = (await _run(authenticated_context, MATERIALIZED, {"id": env["second"]}))["materializedBlok"]
        assert unpinned["pinnedRevision"] is None
        assert unpinned["revision"]["number"] == 3

    async def test_editing_a_provided_blok_forks_it_and_leaves_the_slot(self, authenticated_context: HttpContext) -> None:
        org, user = authenticated_context.request.organization, authenticated_context.request.user

        @sync_to_async
        def provided() -> tuple[Blok, MaterializedBlok, Agent]:
            agent = _agent_in(org, user, "viewer")
            catalog = UICatalog.objects.get_or_create(name="orkestrator", organization=org)[0]
            blok = Blok.objects.create(name="panel", creator=user, organization=org, catalog=catalog, app=agent.app, declaration_hash="a" * 64, components=BOX)
            blok.dependencies.create(key="self")
            slot = MaterializedBlok.objects.create(blok=blok, declared_by=agent, name="panel", description="")
            BlokAgentMapping.objects.create(materialized_blok=slot, key="self", dependency=blok.dependencies.get(), agent=agent)
            # A custom blok of the same name exists already: the fork takes the next free one.
            Blok.objects.create(name="panel", creator=user, organization=org, catalog=catalog)
            return blok, slot, agent

        blok, slot, agent = await provided()
        edited = (await _run(authenticated_context, EDIT_BLOK, {"input": {"materializedBlok": str(slot.id), "components": TEXT, "message": "Make it mine"}}))["editBlok"]
        assert edited["applied"] and edited["forked"]
        assert edited["revision"]["number"] == 2
        assert edited["materializedBlok"]["id"] != str(slot.id)
        assert edited["materializedBlok"]["blok"]["provided"] is False
        assert edited["materializedBlok"]["agentMappings"] == [{"key": "self", "agent": {"id": str(agent.id)}}]

        fork = await sync_to_async(Blok.objects.get)(id=edited["materializedBlok"]["blok"]["id"])
        assert fork.name == "panel (2)"
        assert _drawn(fork.components) == TEXT
        await sync_to_async(slot.refresh_from_db)()
        assert slot.blok_id == blok.id
        assert (await sync_to_async(Blok.objects.get)(id=blok.id)).components == BOX

    async def test_a_custom_edit_names_its_base(self, authenticated_context: HttpContext) -> None:
        env = await _blok_with_two_materializations(authenticated_context)
        message = await _refused(authenticated_context, EDIT_BLOK, {"input": {"materializedBlok": env["first"], "components": TEXT}})
        assert "changed since no revision" in message

    async def test_a_blok_without_history_is_edited_from_no_base(self, authenticated_context: HttpContext) -> None:
        org, user = authenticated_context.request.organization, authenticated_context.request.user

        @sync_to_async
        def bare() -> MaterializedBlok:
            catalog = UICatalog.objects.get_or_create(name="orkestrator", organization=org)[0]
            blok = Blok.objects.create(name="bare", creator=user, organization=org, catalog=catalog, components=BOX)
            return MaterializedBlok.objects.create(blok=blok, name="bare", description="")

        mblok = await bare()
        edited = (await _run(authenticated_context, EDIT_BLOK, {"input": {"materializedBlok": str(mblok.id), "components": TEXT}}))["editBlok"]
        assert edited["revision"]["number"] == 1

    async def test_a_task_of_another_client_is_refused(self, authenticated_context: HttpContext) -> None:
        env = await _blok_with_two_materializations(authenticated_context)
        message = await _refused(authenticated_context, EDIT_BLOK, {"input": {"materializedBlok": env["first"], "base": env["head"], "task": "424242"}})
        assert "Task 424242" in message
        assert await _revision_count(env["blok"]) == 1

    async def test_the_task_of_the_callers_own_agent_is_kept_with_the_revision(self, authenticated_context: HttpContext) -> None:
        env = await _blok_with_two_materializations(authenticated_context)
        request = authenticated_context.request

        @sync_to_async
        def own_task() -> str:
            """A task the agent on the request's own client runs: the assistant's turn."""
            release = Release.objects.create(app=App.objects.create(identifier="designer-turns"), version="1.0.0")
            agent = Agent.objects.create(app=release.app, hash="designer-hash", release=release, user=request.user, client=request.client, organization=request.organization)
            return str(_build_task("designer", agent_pk=agent.pk).pk)

        task = await own_task()
        edited = (await _run(authenticated_context, EDIT_BLOK, {"input": {"materializedBlok": env["first"], "base": env["head"], "components": TEXT, "task": task}}))["editBlok"]
        assert edited["revision"]["task"] == {"id": task}

        reverted = (await _run(authenticated_context, REVERT_BLOK, {"input": {"blok": env["blok"], "to": env["head"], "base": edited["revision"]["id"], "task": task}}))["revertBlok"]
        assert reverted["task"] == {"id": task}

    async def test_an_editor_of_the_blok_itself_saves_on_its_head(self, authenticated_context: HttpContext) -> None:
        env = await _blok_with_two_materializations(authenticated_context)

        saved = (await _run(authenticated_context, EDIT_BLOK, {"input": {"blok": env["blok"], "base": env["head"], "components": TEXT, "dependencies": [{"key": "stage"}], "message": "From the editor"}}))["editBlok"]
        assert saved["applied"] and not saved["forked"]
        assert saved["blok"]["id"] == env["blok"] and saved["materializedBlok"] is None
        assert (saved["revision"]["number"], saved["revision"]["message"]) == (2, "From the editor")
        # Binding is for each materialization: every one that follows the head now lacks it.
        for materialized in (env["first"], env["second"]):
            assert (await _run(authenticated_context, MATERIALIZED, {"id": materialized}))["materializedBlok"]["unboundDependencies"] == ["stage"]

        # An assistant saved meanwhile: the editor, still on revision 1, is refused.
        message = await _refused(authenticated_context, EDIT_BLOK, {"input": {"blok": env["blok"], "base": env["head"], "demoState": {"x": 1}}})
        assert "changed since revision 1" in message
        assert await _revision_count(env["blok"]) == 2

    async def test_a_blok_binds_no_agents_and_an_edit_names_one_target(self, authenticated_context: HttpContext) -> None:
        env = await _blok_with_two_materializations(authenticated_context)
        bindings = await _refused(authenticated_context, EDIT_BLOK, {"input": {"blok": env["blok"], "base": env["head"], "agentMappings": []}})
        assert "binds no agents" in bindings
        both = await _refused(authenticated_context, EDIT_BLOK, {"input": {"blok": env["blok"], "materializedBlok": env["first"], "base": env["head"]}})
        neither = await _refused(authenticated_context, EDIT_BLOK, {"input": {"base": env["head"]}})
        assert "one of the two" in both and "one of the two" in neither

    async def test_an_editor_of_a_provided_blok_saves_a_copy_and_no_materialization(self, authenticated_context: HttpContext) -> None:
        org, user = authenticated_context.request.organization, authenticated_context.request.user

        @sync_to_async
        def provided() -> Blok:
            agent = _agent_in(org, user, "editor-viewer")
            catalog = UICatalog.objects.get_or_create(name="orkestrator", organization=org)[0]
            blok = Blok.objects.create(name="viewer", creator=user, organization=org, catalog=catalog, app=agent.app, declaration_hash="b" * 64, components=BOX)
            MaterializedBlok.objects.create(blok=blok, declared_by=agent, name="viewer", description="")
            return blok

        blok = await provided()
        materializations = await sync_to_async(MaterializedBlok.objects.count)()

        checked = (await _run(authenticated_context, EDIT_BLOK, {"input": {"blok": str(blok.id), "components": TEXT, "dryRun": True}}))["editBlok"]
        assert (checked["applied"], checked["forked"], checked["blok"]["id"]) == (False, True, str(blok.id))
        assert await sync_to_async(Blok.objects.count)() == 1

        saved = (await _run(authenticated_context, EDIT_BLOK, {"input": {"blok": str(blok.id), "components": TEXT}}))["editBlok"]
        assert saved["forked"] and saved["materializedBlok"] is None
        assert saved["blok"]["id"] != str(blok.id) and saved["blok"]["provided"] is False and saved["blok"]["name"] == "viewer"
        assert saved["revision"]["number"] == 2
        assert await sync_to_async(MaterializedBlok.objects.count)() == materializations
        assert _drawn((await sync_to_async(Blok.objects.get)(id=blok.id)).components) == BOX

    async def test_deleting_a_blok_takes_its_history(self, authenticated_context: HttpContext) -> None:
        env = await _blok_with_two_materializations(authenticated_context)
        await _run(authenticated_context, EDIT_BLOK, {"input": {"materializedBlok": env["first"], "base": env["head"], "components": TEXT}})
        await sync_to_async(Blok.objects.filter(id=env["blok"]).delete)()
        assert await _revision_count(env["blok"]) == 0
