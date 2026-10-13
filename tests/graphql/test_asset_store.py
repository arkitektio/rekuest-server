"""The asset store: an agent uploads the files it declares, each held to the rules of its kind."""

import struct

import pytest
from asgiref.sync import sync_to_async
from authentikate.models import App, Release
from kante.context import HttpContext

from datalayer.datalayer import get_current_datalayer
from datalayer.models import MediaStore
from facade.glb_validation import model_ref
from facade.models import Agent, AgentAsset, Asset
from facade.schema import schema
from tests.graphql.test_cross_tenant_isolation import OTHER_TOKEN, tenant_context
from tests.models.test_glb_validation import glb, triangle

REQUEST = "mutation($input: RequestAssetUploadInput!) { requestAssetUpload(input: $input) { store bucket key } }"
CREATE = "mutation($input: CreateAssetInput!) { createAsset(input: $input) { id ref kind size nodes stats { nodes triangles } width height } }"
ASSET = "query($ref: String!) { asset(ref: $ref) { id ref } }"
AGENT_ASSETS = "query($id: ID!) { agent(id: $id) { assets { name kind ref size source asset { id ref } } } }"
SOURCE = "https://vendor.example/models/scope.glb"
POLICY = "query { assetPolicy { model { maxSize maxNodes extensions } image { maxSize maxSide formats } } }"


def png(width: int = 64, height: int = 32) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00rest"


def _caller(token: str) -> tuple[HttpContext, Agent]:
    """A context and the agent it registers as, which is the one whose declaration counts."""
    context, user, client, organization = tenant_context(token)
    app, _ = App.objects.get_or_create(identifier=f"asset-app-{token}")
    release, _ = Release.objects.get_or_create(app=app, version="1.0.0")
    agent, _ = Agent.objects.get_or_create(user=user, client=client, organization=organization, defaults={"app": app, "release": release, "hash": "assets"})
    return context, agent


@pytest.fixture
def caller(db, backend_stack) -> tuple[HttpContext, Agent]:
    return _caller("test-member")


@pytest.fixture
def stranger(db, backend_stack) -> tuple[HttpContext, Agent]:
    return _caller(OTHER_TOKEN)


@pytest.fixture
def admin(db, backend_stack) -> tuple[HttpContext, Agent]:
    """An admin of the caller's organization (a static token is one unless it says otherwise), with an agent of their own."""
    return _caller("test")


@pytest.fixture
def media_bucket(s3, settings):
    """A datalayer with a media bucket, on moto."""
    from datalayer import datalayer as module

    settings.DATALAYER = {"allow_unscoped_fallback": True, "access_key": "testing", "secret_key": "testing", "media": {"path": "media"}}
    module.GLOBAL_DL = None
    get_current_datalayer()._s3.create_bucket(Bucket="media")
    yield
    module.GLOBAL_DL = None


async def _declare(agent: Agent, name: str, data: bytes, kind: str, ref: str | None = None) -> str:
    """What registration writes for an agent that brings ``data`` as ``name``; its reference."""
    ref = ref or model_ref(data)
    await sync_to_async(AgentAsset.objects.create)(agent=agent, name=name, kind=kind, ref=ref, size=len(data))
    return ref


async def _name_source(agent: Agent, name: str, ref: str, kind: str = "MODEL_3D") -> None:
    """What registration writes for an agent that holds no file and names where to fetch it."""
    await sync_to_async(AgentAsset.objects.create)(agent=agent, name=name, kind=kind, ref=ref, size=None, source=SOURCE)


async def _upload(context: HttpContext, data: bytes, ref: str, size: int | None = None) -> str:
    """Ask for a grant and put ``data`` where it points, as an uploading client does; the store id."""
    granted = await schema.execute(REQUEST, context_value=context, variable_values={"input": {"ref": ref, **({"size": size} if size is not None else {})}})
    assert not granted.errors, granted.errors
    grant = granted.data["requestAssetUpload"]
    await sync_to_async(get_current_datalayer()._s3.put_object)(Bucket=grant["bucket"], Key=grant["key"], Body=data)
    return grant["store"]


async def _create(context: HttpContext, ref: str, store: str):
    return await schema.execute(CREATE, context_value=context, variable_values={"input": {"ref": ref, "store": store}})


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestAssetStore:
    async def test_an_agent_uploads_the_model_it_declares_once(self, caller: tuple[HttpContext, Agent], media_bucket: None) -> None:
        context, agent = caller
        data = glb(*triangle())
        ref = await _declare(agent, "scope", data, "MODEL_3D")
        assert (await schema.execute(ASSET, context_value=context, variable_values={"ref": ref})).data == {"asset": None}

        created = await _create(context, ref, await _upload(context, data, ref))
        assert not created.errors, created.errors
        asset = created.data["createAsset"]
        assert (asset["ref"], asset["kind"], asset["size"]) == (ref, "MODEL_3D", len(data))
        assert asset["nodes"] == ["stage_x", "turret"] and asset["stats"] == {"nodes": 2, "triangles": 1}

        again = await _upload(context, data, ref)
        repeated = await _create(context, ref, again)
        assert repeated.data["createAsset"]["id"] == asset["id"]
        assert await sync_to_async(Asset.objects.filter(ref=ref).count)() == 1
        assert not await sync_to_async(MediaStore.objects.filter(id=again).exists)(), "the surplus upload was kept"

        listed = await schema.execute(AGENT_ASSETS, context_value=context, variable_values={"id": str(agent.pk)})
        assert listed.data["agent"]["assets"] == [{"name": "scope", "kind": "MODEL_3D", "ref": ref, "size": len(data), "source": None, "asset": {"id": asset["id"], "ref": ref}}]

    async def test_an_image_is_an_asset_too(self, caller: tuple[HttpContext, Agent], media_bucket: None) -> None:
        context, agent = caller
        data = png()
        ref = await _declare(agent, "logo", data, "IMAGE")
        created = await _create(context, ref, await _upload(context, data, ref))
        assert not created.errors, created.errors
        asset = created.data["createAsset"]
        assert (asset["kind"], asset["width"], asset["height"], asset["nodes"], asset["stats"]) == ("IMAGE", 64, 32, [], None)

    async def test_a_file_no_agent_of_the_caller_declares_is_refused(self, caller: tuple[HttpContext, Agent], stranger: tuple[HttpContext, Agent], media_bucket: None) -> None:
        context, agent = caller
        data = glb(*triangle())
        undeclared = await schema.execute(REQUEST, context_value=context, variable_values={"input": {"ref": model_ref(data)}})
        assert undeclared.errors and undeclared.errors[0].extensions == {"code": "not_declared"}

        ref = await _declare(agent, "scope", data, "MODEL_3D")
        store = await _upload(context, data, ref)
        # Another organization's agent declares nothing of the kind, and may not record this one's upload.
        # Its token is an admin's, so the refusal names what an admin may upload.
        theirs = await schema.execute(REQUEST, context_value=stranger[0], variable_values={"input": {"ref": ref}})
        assert theirs.errors and theirs.errors[0].extensions == {"code": "not_sourced"}
        assert (await _create(stranger[0], ref, store)).errors

        assert not (await _create(context, ref, store)).errors
        assert (await schema.execute(ASSET, context_value=stranger[0], variable_values={"ref": ref})).data == {"asset": None}

    async def test_bytes_that_do_not_hash_to_the_reference_are_refused_and_removed(self, caller: tuple[HttpContext, Agent], media_bucket: None) -> None:
        context, agent = caller
        data = glb(*triangle())
        claimed = await _declare(agent, "scope", data, "MODEL_3D", ref="sha256:" + "0" * 64)
        store = await _upload(context, data, claimed)
        refused = await _create(context, claimed, store)
        assert refused.errors and refused.errors[0].extensions == {"code": "hash_mismatch"}
        assert not await sync_to_async(MediaStore.objects.filter(id=store).exists)()
        assert not await sync_to_async(Asset.objects.filter(ref=claimed).exists)()

    async def test_a_file_is_held_to_the_rules_of_the_kind_it_is_declared_as(self, caller: tuple[HttpContext, Agent], media_bucket: None) -> None:
        context, agent = caller
        document, binary = triangle()
        external = glb({**document, "images": [{"uri": "https://example.org/t.png"}]}, binary)
        ref = await _declare(agent, "external", external, "MODEL_3D")
        refused = await _create(context, ref, await _upload(context, external, ref))
        assert refused.errors and refused.errors[0].extensions == {"code": "external_reference"}

        svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
        ref = await _declare(agent, "drawing", svg, "IMAGE")
        refused = await _create(context, ref, await _upload(context, svg, ref))
        assert refused.errors and refused.errors[0].extensions == {"code": "bad_header"}

        model_as_image = glb(*triangle())
        ref = await _declare(agent, "mislabelled", model_as_image, "IMAGE")
        refused = await _create(context, ref, await _upload(context, model_as_image, ref))
        assert refused.errors and refused.errors[0].extensions == {"code": "bad_header"}

        huge = png(width=20000)
        ref = await _declare(agent, "huge", huge, "IMAGE")
        refused = await _create(context, ref, await _upload(context, huge, ref))
        assert refused.errors and refused.errors[0].extensions == {"code": "over_budget"}

    async def test_a_declared_size_over_the_limit_gets_no_grant(self, caller: tuple[HttpContext, Agent], media_bucket: None) -> None:
        context, agent = caller
        ref = "sha256:" + "a" * 64
        await sync_to_async(AgentAsset.objects.create)(agent=agent, name="big", kind="IMAGE", ref=ref, size=11 * 1024 * 1024)
        too_large = await schema.execute(REQUEST, context_value=context, variable_values={"input": {"ref": ref}})
        assert too_large.errors and too_large.errors[0].extensions == {"code": "too_large"}
        bad_ref = await schema.execute(REQUEST, context_value=context, variable_values={"input": {"ref": "md5:abc"}})
        assert bad_ref.errors and bad_ref.errors[0].extensions == {"code": "bad_reference"}

    async def test_an_admin_uploads_the_file_an_agent_names_a_source_for(self, caller: tuple[HttpContext, Agent], admin: tuple[HttpContext, Agent], media_bucket: None) -> None:
        member, agent = caller
        data = glb(*triangle())
        ref = model_ref(data)
        await _name_source(agent, "scope", ref)

        declared = (await schema.execute(AGENT_ASSETS, context_value=member, variable_values={"id": str(agent.pk)})).data["agent"]["assets"]
        assert declared == [{"name": "scope", "kind": "MODEL_3D", "ref": ref, "size": None, "source": SOURCE, "asset": None}]

        # The declaration has no size: whoever fetched the file says how large it is.
        unsized = await schema.execute(REQUEST, context_value=admin[0], variable_values={"input": {"ref": ref}})
        assert unsized.errors and unsized.errors[0].extensions == {"code": "bad_header"}

        store = await _upload(admin[0], data, ref, size=len(data))
        created = await _create(admin[0], ref, store)
        assert not created.errors, created.errors
        assert created.data["createAsset"]["ref"] == ref and created.data["createAsset"]["size"] == len(data)

        stored = (await schema.execute(AGENT_ASSETS, context_value=member, variable_values={"id": str(agent.pk)})).data["agent"]["assets"]
        assert stored[0]["asset"]["ref"] == ref

    async def test_only_an_admin_uploads_for_another_agent_and_only_a_sourced_file(self, caller: tuple[HttpContext, Agent], admin: tuple[HttpContext, Agent], stranger: tuple[HttpContext, Agent], media_bucket: None) -> None:
        member, _ = caller
        data, held = glb(*triangle()), png()
        sourced, unsourced = model_ref(data), model_ref(held)
        await _name_source(admin[1], "scope", sourced)
        await _declare(admin[1], "logo", held, "IMAGE")

        # A member who is no admin uploads only what their own agent declares.
        refused = await schema.execute(REQUEST, context_value=member, variable_values={"input": {"ref": sourced, "size": len(data)}})
        assert refused.errors and refused.errors[0].extensions == {"code": "not_declared"}

        # An admin of another organization sees no source here.
        theirs = await schema.execute(REQUEST, context_value=stranger[0], variable_values={"input": {"ref": sourced, "size": len(data)}})
        assert theirs.errors and theirs.errors[0].extensions == {"code": "not_sourced"}

        # A file an agent holds is that agent's to upload, an admin's or not.
        other_admin, _ = await sync_to_async(_caller)("test2")
        held_by_agent = await schema.execute(REQUEST, context_value=other_admin, variable_values={"input": {"ref": unsourced, "size": len(held)}})
        assert held_by_agent.errors and held_by_agent.errors[0].extensions == {"code": "not_sourced"}

    async def test_a_fetched_file_is_held_to_the_same_rules(self, caller: tuple[HttpContext, Agent], admin: tuple[HttpContext, Agent], media_bucket: None) -> None:
        _, agent = caller
        data = glb(*triangle())
        claimed = "sha256:" + "d" * 64
        await _name_source(agent, "scope", claimed)

        too_large = await schema.execute(REQUEST, context_value=admin[0], variable_values={"input": {"ref": claimed, "size": 51 * 1024 * 1024}})
        assert too_large.errors and too_large.errors[0].extensions == {"code": "too_large"}

        store = await _upload(admin[0], data, claimed, size=len(data))
        refused = await _create(admin[0], claimed, store)
        assert refused.errors and refused.errors[0].extensions == {"code": "hash_mismatch"}
        assert not await sync_to_async(MediaStore.objects.filter(id=store).exists)()

    async def test_the_policy_names_the_limits_of_each_kind(self, caller: tuple[HttpContext, Agent]) -> None:
        policy = (await schema.execute(POLICY, context_value=caller[0])).data["assetPolicy"]
        assert policy["model"]["maxSize"] == 50 * 1024 * 1024 and policy["image"] == {"maxSize": 10 * 1024 * 1024, "maxSide": 8192, "formats": ["png", "jpeg", "webp"]}
