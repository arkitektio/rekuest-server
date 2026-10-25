"""A blok's persisted locals: the ``demo_state`` keys a renderer may keep between openings.

Only the declaration lives here. The values stay on the device that set them, so the server
stores ``persist`` with the blok and its revisions, counts it in the content, and refuses a key
that is not a local. ``editBlok`` keeps it when omitted and an empty list removes it; a revert
brings an older list back; a fork copies the provided blok's.
"""

import pytest
from asgiref.sync import sync_to_async
from authentikate.models import App
from kante.context import HttpContext

from facade.blok_revisions import BlokContent
from facade.models import Blok, UICatalog
from facade.schema import schema
from rekuest_core import enums
from rekuest_core.inputs import models as rimodels

BOX = [{"id": "root", "component": "Box"}]
LOCALS = {"pose": [1, 0, 0, 0], "zoom": 1}
POSE = {"key": "pose", "description": "Where the gizmo left it"}

CREATE = """
mutation CreateBlok($input: CreateBlokInput!) {
  createBlok(input: $input) { id persist { key description } head { id persist { key description } } }
}
"""

EDIT = """
mutation EditBlok($input: EditBlokInput!) {
  editBlok(input: $input) { applied blok { id persist { key description } } revision { id number persist { key } } }
}
"""

REVERT = """
mutation RevertBlok($input: RevertBlokInput!) {
  revertBlok(input: $input) { id number persist { key description } }
}
"""

FORK = """
mutation ForkBlok($input: ForkBlokInput!) {
  forkBlok(input: $input) { id provided persist { key description } head { persist { key } } }
}
"""

BLOK = """
query Blok($id: ID!) { blok(id: $id) { persist { key description } head { id number } } }
"""


async def _run(context: HttpContext, operation: str, variables: dict) -> dict:
    result = await schema.execute(operation, context_value=context, variable_values=variables)
    assert not result.errors, result.errors
    assert result.data is not None
    return result.data


async def _refused(context: HttpContext, operation: str, variables: dict) -> str:
    result = await schema.execute(operation, context_value=context, variable_values=variables)
    assert result.errors, "expected the write to be refused"
    return result.errors[0].message


def test_only_locals_are_persisted() -> None:
    rimodels.check_blok_persist([rimodels.BlokPersistInputModel(key="pose")], set(), {"pose"})
    with pytest.raises(ValueError, match="persist names 'speed', which is not a demo_state key"):
        rimodels.check_blok_persist([rimodels.BlokPersistInputModel(key="speed")], set(), {"pose"})
    with pytest.raises(ValueError, match="persist names 'pose', which is not a demo_state key"):
        rimodels.check_blok_persist([rimodels.BlokPersistInputModel(key="pose")], set(), None)
    with pytest.raises(ValueError, match="duplicate key 'pose'"):
        rimodels.check_blok_persist([rimodels.BlokPersistInputModel(key="pose")] * 2, set(), {"pose"})
    for root in ("args", "$event", "stage"):
        with pytest.raises(ValueError, match="not a local but a root the renderer fills"):
            rimodels.check_blok_persist([rimodels.BlokPersistInputModel(key=root)], {"stage"}, {root})


def test_an_agent_declaration_names_only_its_locals() -> None:
    declared = {"key": "posed", "components": BOX, "demo_state": LOCALS}
    assert rimodels.BlokImplementationInputModel.model_validate({**declared, "persist": [POSE]}).persist[0].key == "pose"
    assert rimodels.BlokImplementationInputModel.model_validate(declared).persist == []
    with pytest.raises(ValueError, match="not a demo_state key"):
        rimodels.BlokImplementationInputModel.model_validate({**declared, "persist": [{"key": "speed"}]})


def test_no_persisted_locals_leave_the_content_hash_as_it_was() -> None:
    """Persisted locals count in the revision hash only when there are some, so older revisions keep theirs."""
    catalog = type("Catalog", (), {"pk": 1})()
    content = BlokContent(components=[], demo_state=dict(LOCALS), context=enums.ComponentContext.HTML, catalog=catalog, dependencies=[])  # type: ignore[arg-type]
    assert content.but(persist=[]).hash() == content.hash()
    assert content.but(persist=[rimodels.BlokPersistInputModel(key="pose")]).hash() != content.hash()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestBlokPersist:
    async def test_declare_edit_and_revert(self, authenticated_context: HttpContext) -> None:
        created = (await _run(authenticated_context, CREATE, {"input": {"name": "posed", "components": BOX, "demoState": LOCALS, "persist": [POSE]}}))["createBlok"]
        assert created["persist"] == [POSE]
        assert created["head"]["persist"] == [POSE]
        blok, first = created["id"], created["head"]["id"]

        # Omitted: kept. A content change without it keeps the list.
        kept = (await _run(authenticated_context, EDIT, {"input": {"blok": blok, "base": first, "demoState": {**LOCALS, "zoom": 2}}}))["editBlok"]
        assert kept["blok"]["persist"] == [POSE]
        assert kept["revision"]["persist"] == [{"key": "pose"}]

        # Only the list changes: still a new revision, since persist is content.
        both = [POSE, {"key": "zoom", "description": None}]
        grown = (await _run(authenticated_context, EDIT, {"input": {"blok": blok, "base": kept["revision"]["id"], "persist": both}}))["editBlok"]
        assert grown["revision"]["number"] == 3
        assert grown["blok"]["persist"] == both

        # An empty list removes them.
        cleared = (await _run(authenticated_context, EDIT, {"input": {"blok": blok, "base": grown["revision"]["id"], "persist": []}}))["editBlok"]
        assert cleared["blok"]["persist"] == []

        reverted = (await _run(authenticated_context, REVERT, {"input": {"blok": blok, "to": first, "base": cleared["revision"]["id"]}}))["revertBlok"]
        assert reverted["number"] == 5
        assert reverted["persist"] == [POSE]
        assert (await _run(authenticated_context, BLOK, {"id": blok}))["blok"]["persist"] == [POSE]

    async def test_a_key_that_is_not_a_local_is_refused(self, authenticated_context: HttpContext) -> None:
        message = await _refused(authenticated_context, CREATE, {"input": {"name": "posed", "components": BOX, "demoState": LOCALS, "persist": [{"key": "speed"}]}})
        assert "persist names 'speed', which is not a demo_state key" in message
        assert not await sync_to_async(Blok.objects.filter(name="posed").exists)()

        created = (await _run(authenticated_context, CREATE, {"input": {"name": "posed", "components": BOX, "demoState": LOCALS, "persist": [POSE]}}))["createBlok"]
        twice = await _refused(authenticated_context, EDIT, {"input": {"blok": created["id"], "base": created["head"]["id"], "persist": [POSE, POSE]}})
        assert "duplicate key 'pose'" in twice
        # A local the edit drops from the demo state may not stay persisted.
        dropped = await _refused(authenticated_context, EDIT, {"input": {"blok": created["id"], "base": created["head"]["id"], "demoState": {"zoom": 1}}})
        assert "persist names 'pose'" in dropped
        # Created again without the local: the kept list no longer fits.
        again = await _refused(authenticated_context, CREATE, {"input": {"name": "posed", "components": BOX, "demoState": {"zoom": 1}}})
        assert "persist names 'pose'" in again

    async def test_created_again_keeps_the_list_unless_given(self, authenticated_context: HttpContext) -> None:
        await _run(authenticated_context, CREATE, {"input": {"name": "posed", "components": BOX, "demoState": LOCALS, "persist": [POSE]}})
        again = (await _run(authenticated_context, CREATE, {"input": {"name": "posed", "components": BOX, "demoState": LOCALS}}))["createBlok"]
        assert again["persist"] == [POSE]
        cleared = (await _run(authenticated_context, CREATE, {"input": {"name": "posed", "components": BOX, "demoState": LOCALS, "persist": []}}))["createBlok"]
        assert cleared["persist"] == []

    async def test_a_fork_keeps_the_provided_list(self, authenticated_context: HttpContext) -> None:
        @sync_to_async
        def provided() -> Blok:
            org, user = authenticated_context.request.organization, authenticated_context.request.user
            catalog = UICatalog.objects.get_or_create(name="orkestrator", organization=org)[0]
            return Blok.objects.create(
                name="posed",
                creator=user,
                organization=org,
                catalog=catalog,
                app=App.objects.create(identifier="viewer"),
                declaration_hash="a" * 64,
                components=BOX,
                demo_state=LOCALS,
                persist=[POSE],
            )

        blok = await provided()
        fork = (await _run(authenticated_context, FORK, {"input": {"id": str(blok.id), "name": "my-posed"}}))["forkBlok"]
        assert fork["provided"] is False
        assert fork["persist"] == [POSE]
        assert fork["head"]["persist"] == [{"key": "pose"}]
