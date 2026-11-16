"""UI catalog registry: registration, tenant isolation, and validation of bloks against it."""

import itertools

import pytest
from asgiref.sync import sync_to_async
from kante.context import HttpContext

from facade.models import Module
from facade.schema import schema
from tests.graphql.test_cross_tenant_isolation import OTHER_TOKEN, tenant_context
from tests.graphql_ops import CREATE_BLOK
from tests.registered import blok, gql, local, node

REGISTER_UI_CATALOG = """
    mutation RegisterModule($input: RegisterModuleInput!) {
        registerModule(input: $input) {
            id
            name
            description
            isRegistered
            components { name props { key kind required } acceptsChildren }
            operations { name arguments { key kind required } returns }
        }
    }
"""

UI_CATALOGS = "query { modules { id name } }"
UI_CATALOG = "query UiCatalog($id: ID!) { module(id: $id) { id name } }"

CATALOG = {
    "name": "electron",
    "description": "The desktop app",
    "components": [
        {"name": "Dial", "props": [{"key": "value", "kind": "FLOAT", "required": True}, {"key": "onChange", "kind": "CALLBACK"}], "acceptsChildren": False},
        {"name": "Frame"},
    ],
    "operations": [
        {"name": "clamp", "arguments": [{"key": "a", "kind": "FLOAT"}, {"key": "b", "kind": "FLOAT"}], "returns": "FLOAT"},
        {"name": "fmt.dial", "arguments": [{"key": "v", "kind": "ANY"}], "returns": "STRING"},
    ],
}

BASE_CATALOG = "query { baseCatalog { name version operations { name returns arguments { key kind required } } } }"


async def _blok_diagnostics(name: str = "catalogued") -> list[dict]:
    from facade.models import Blok

    return await sync_to_async(lambda: Blok.objects.get(name=name).diagnostics)()


async def _stored(result: object, name: str = "catalogued") -> list[dict]:
    """The findings a blok was stored with: a catalog finding never refuses it."""
    assert not result.errors, result.errors  # type: ignore[attr-defined]
    return await _blok_diagnostics(name)


def _unchecked(findings: list[dict]) -> None:
    """The server holds a blok to the language alone: what a catalog says of it (an unknown
    component, a prop, a placement) is checked by the frontend that draws it, never here."""
    assert not [finding for finding in findings if finding["code"].startswith(("catalog.", "module."))], findings


def _found(findings: list[dict], code: str, text: str) -> None:
    assert any(finding["code"] == code and text in finding["message"] for finding in findings), findings


async def _register(context: HttpContext, catalog: dict = CATALOG) -> dict:
    result = await schema.execute(REGISTER_UI_CATALOG, context_value=context, variable_values={"input": catalog})
    assert not result.errors, result.errors
    return result.data["registerModule"]


SPELLED = {"PANEL": "panel", "SCENE": "scene", "LOGO": "logo"}
EXPOSURE = local("exposure", kind="INT", default=1)


def _tree(*content: dict, context: str = "PANEL", declarations: tuple[dict, ...] = (EXPOSURE,)) -> list[dict]:
    """A custom blok's tree: one ``<Blok kind="custom">`` holding its declarations, then ``content``."""
    return [gql(blok("custom", SPELLED[context], *declarations, *content))]


async def _create_blok(http: HttpContext, content: list[dict], catalog: str = "electron", *, context: str = "PANEL", components: list[dict] | None = None, **extra: object):
    """A custom blok holding ``content`` beside its local ``exposure``; ``components`` is sent as it is instead."""
    tree = components if components is not None else _tree(*content, context=context)
    return await schema.execute(
        CREATE_BLOK,
        context_value=http,
        variable_values={"input": {"name": "catalogued", "vocab": 2, "module": catalog, "components": tree, "context": context, **extra}},
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestUiCatalog:
    async def test_register_upserts_by_name_within_the_organization(self, authenticated_context: HttpContext) -> None:
        """Registering twice under one name updates the same row and replaces its contents."""
        first = await _register(authenticated_context)
        assert first["isRegistered"] is True
        assert {c["name"] for c in first["components"]} == {"Dial", "Frame"}
        assert first["components"][0]["props"][0] == {"key": "value", "kind": "FLOAT", "required": True}
        assert first["operations"][0]["returns"] == "FLOAT"

        second = await _register(authenticated_context, {**CATALOG, "components": [{"name": "Text"}], "operations": []})
        assert second["id"] == first["id"]
        assert [c["name"] for c in second["components"]] == ["Text"]
        assert second["operations"] == []
        assert await sync_to_async(Module.objects.filter(name="electron").count)() == 1

    async def test_a_ui_names_the_catalogs_it_implements(self, authenticated_context: HttpContext) -> None:
        """``catalogs`` is stored as registered and read back; registering again replaces it."""
        query = REGISTER_UI_CATALOG.replace("isRegistered", "isRegistered catalogs")
        result = await schema.execute(query, context_value=authenticated_context, variable_values={"input": {**CATALOG, "catalogs": ["ui@1", "scene@1"]}})
        assert not result.errors, result.errors
        assert result.data["registerModule"]["catalogs"] == ["ui@1", "scene@1"]
        assert await sync_to_async(lambda: Module.objects.get(name="electron").catalogs)() == ["ui@1", "scene@1"]
        again = await schema.execute(query, context_value=authenticated_context, variable_values={"input": CATALOG})
        assert again.data["registerModule"]["catalogs"] == []

    async def test_a_module_is_spelled_name_at_version_once(self, authenticated_context: HttpContext) -> None:
        for catalogs, code in ((["ui"], "[manifest.catalog]"), (["ui@1", "ui@1"], "[manifest.duplicate]")):
            result = await schema.execute(REGISTER_UI_CATALOG, context_value=authenticated_context, variable_values={"input": {**CATALOG, "catalogs": catalogs}})
            assert result.errors and code in result.errors[0].message, (catalogs, result.errors)
        assert await sync_to_async(Module.objects.filter(name="electron").count)() == 0

    async def test_catalogs_are_scoped_to_the_organization(self, authenticated_context: HttpContext) -> None:
        """Another organization may register the same name and neither sees the other's catalog."""
        other, _, _, _ = await sync_to_async(tenant_context)(OTHER_TOKEN)

        mine = await _register(authenticated_context)
        theirs = await _register(other)
        assert mine["id"] != theirs["id"]

        listed = await schema.execute(UI_CATALOGS, context_value=authenticated_context)
        assert not listed.errors, listed.errors
        assert {row["id"] for row in listed.data["modules"]} == {mine["id"]}

        foreign = await schema.execute(UI_CATALOG, context_value=authenticated_context, variable_values={"id": theirs["id"]})
        assert foreign.errors, "org A read org B's catalog"

    async def test_blok_against_registered_catalog_is_validated(self, authenticated_context: HttpContext) -> None:
        """What a catalog says of a blok is not checked here (the frontend that draws it does); a language error refuses it."""
        await _register(authenticated_context)

        unknown_component = await _create_blok(authenticated_context, [{"id": "root", "component": "Knob"}])
        _unchecked(await _stored(unknown_component))

        unknown_prop = await _create_blok(authenticated_context, [{"id": "root", "component": "Dial", "props": [{"key": "value", "staticValue": 1}, {"key": "colour", "staticValue": "red"}]}])
        _unchecked(await _stored(unknown_prop))

        missing_required = await _create_blok(authenticated_context, [{"id": "root", "component": "Dial"}])
        _unchecked(await _stored(missing_required))

        children_refused = await _create_blok(authenticated_context, [{"id": "root", "component": "Dial", "props": [{"key": "value", "staticValue": 1}], "children": [{"id": "c", "component": "Frame"}]}])
        _unchecked(await _stored(children_refused))

        unknown_function = await _create_blok(
            authenticated_context,
            [{"id": "root", "component": "Dial", "props": [{"key": "value", "utilCall": {"operation": "fmt.fizz", "arguments": [{"key": "v", "valuePath": "exposure"}]}}]}],
        )
        # A catalog may add functions, so one the language does not know is the frontend's to tell.
        assert not unknown_function.errors, unknown_function.errors
        assert [d["code"] for d in await _blok_diagnostics()] == []

        # A function of the language called wrongly refuses the blok.
        refused = await _create_blok(authenticated_context, [{"id": "root", "component": "Dial", "props": [{"key": "value", "utilCall": {"operation": "cmp.gt", "arguments": [{"key": "a", "valuePath": "exposure"}]}}]}])
        assert refused.errors and "cmp.gt: needs b" in refused.errors[0].message, refused.errors

        # Which props of a component are callbacks is its catalog's to say: a value in one is the frontend's to tell.
        in_callback = await _create_blok(authenticated_context, [{"id": "root", "component": "Dial", "props": [{"key": "value", "staticValue": 1}, {"key": "onChange", "utilCall": {"operation": "fmt.dial", "arguments": [{"key": "v", "valuePath": "exposure"}]}}]}])
        assert not in_callback.errors, in_callback.errors

        accepted = await _create_blok(
            authenticated_context,
            [
                {
                    "id": "root",
                    "component": "Frame",
                    "children": [
                        {
                            "id": "dial",
                            "component": "Dial",
                            "props": [
                                {"key": "value", "utilCall": {"operation": "cmp.gt", "arguments": [{"key": "a", "valuePath": "exposure"}, {"key": "b", "valueLiteral": 0}]}},
                                {"key": "onChange", "utilCall": {"operation": "ui.copy", "arguments": [{"key": "text", "valuePath": "event.value"}]}},
                            ],
                        }
                    ],
                }
            ],
        )
        assert not accepted.errors, accepted.errors
        assert await _blok_diagnostics() == []

    async def test_a_blok_naming_no_catalog_is_checked_against_the_standard_one(self, authenticated_context: HttpContext) -> None:
        """Without a catalog name a blok is held to ``orkestrator``: the spec's catalogs plus what it registered."""
        await _register(authenticated_context, {**CATALOG, "name": "orkestrator"})
        unnamed = {"name": "catalogued", "vocab": 2}

        unknown = await schema.execute(CREATE_BLOK, context_value=authenticated_context, variable_values={"input": {**unnamed, "components": _tree({"id": "root", "component": "Knob"}, declarations=())}})
        _unchecked(await _stored(unknown))

        for component in ("Frame", "Card"):
            accepted = await schema.execute(CREATE_BLOK, context_value=authenticated_context, variable_values={"input": {**unnamed, "components": _tree({"id": "root", "component": component}, declarations=())}})
            assert await _stored(accepted) == [], component

    async def test_components_are_held_to_their_declared_placement(self, authenticated_context: HttpContext) -> None:
        """A component outside its context, or without a required ancestor, is a placement warning."""
        await _register(authenticated_context, {**CATALOG, "components": [{"name": "Frame"}, {"name": "Caption", "acceptsChildren": False}]})

        ids = itertools.count()

        def element(component: str, *children: dict, props: list[dict] | None = None) -> dict:
            return {"id": f"{component}{next(ids)}", "component": component, "props": props, "children": list(children)}

        moved = [{"key": "node", "staticValue": "arm"}, {"key": "axis", "staticValue": "x"}]
        model = [{"key": "src", "dynamicValue": {"path": "deps.robot.assets.arm"}}]
        accepted = await _create_blok(authenticated_context, [element("Frame", element("Scene", element("Model", element("Move", props=moved), element("Popout", element("Frame", element("Caption"))), props=model)))], dependencies=[{"key": "robot"}])
        assert await _stored(accepted) == []

        for tree, message in (
            (element("Frame", element("Move", props=moved)), "<Move> is a scene component, not one of the panel context"),
            (element("Frame", element("Scene", element("Caption"))), "<Caption> is a panel component, not one of the scene context"),
            (element("Frame", element("Scene", element("Move", props=moved))), "<Move> must be inside one of ['Model', 'Robot']"),
        ):
            misplaced = await _create_blok(authenticated_context, [tree])
            _unchecked(await _stored(misplaced))

    async def test_a_scene_blok_opens_a_scene_and_names_its_models_properly(self, authenticated_context: HttpContext) -> None:
        """A scene blok holds one Scene (or Space); a model names an asset of a declared dependency."""
        await _register(authenticated_context, {**CATALOG, "components": [{"name": "Frame"}]})

        def scene(src: dict) -> list[dict]:
            return [{"id": "scene", "component": "Scene", "children": [{"id": "model", "component": "Model", "props": [{"key": "src", **src}]}]}]

        named = {"dynamicValue": {"path": "deps.robot.assets.scope"}}
        robot = [{"key": "robot"}]

        ref = "sha256:" + "9f" * 32
        accepted = await _create_blok(authenticated_context, scene(named), context="SCENE", dependencies=robot)
        assert await _stored(accepted) == []
        from facade.models import Blok

        assert await sync_to_async(lambda: Blok.objects.get(name="catalogued").context)() == "SCENE"

        for content, catalog, message in (
            ([{"id": "root", "component": "Frame"}], "electron", "A scene blok holds one <Scene> or one <Space> beside its declarations, not <Frame>"),
            ([{"id": "root", "component": "Frame", "children": scene(named)}], "electron", "A scene blok holds one <Scene> or one <Space> beside its declarations, not <Frame>"),
            # A catalog nothing registered in: what a scene holds is still the vocabulary's.
            ([{"id": "root", "component": "Frame"}], "unregistered", "A scene blok holds one <Scene> or one <Space> beside its declarations, not <Frame>"),
            ([*scene(named), {"id": "other", "component": "Scene"}], "electron", "not <Scene>, <Scene>"),
            (scene({"dynamicValue": {"path": "deps.robot.assets"}}), "electron", "an asset is <agent>.assets.<name>"),
        ):
            refused = await _create_blok(authenticated_context, content, module=catalog, context="SCENE", dependencies=robot)
            assert refused.errors and message in refused.errors[0].message, refused.errors
        two_roots = await _create_blok(authenticated_context, [], components=[*_tree(*scene(named), context="SCENE"), {"id": "other", "component": "Scene"}], context="SCENE", dependencies=robot)
        assert two_roots.errors and "a blok has one root, not 2" in two_roots.errors[0].message, two_roots.errors
        # A custom blok reads no `self`; an asset is a dependency's.
        of_nobody = await _create_blok(authenticated_context, scene({"dynamicValue": {"path": "self.assets.scope"}}), context="SCENE")
        assert of_nobody.errors and "a custom blok has no 'self'" in of_nobody.errors[0].message, of_nobody.errors
        # A model named by a hash, not an asset, is the catalog's finding.
        by_hash = await _create_blok(authenticated_context, scene({"staticValue": ref}), context="SCENE")
        _unchecked(await _stored(by_hash))

        decorating_scene = await _create_blok(authenticated_context, scene(named), context="SCENE", dependencies=robot, forModel=ref)
        assert decorating_scene.errors and "only a panel blok can decorate a model" in decorating_scene.errors[0].message
        decorating = await _create_blok(authenticated_context, [{"id": "root", "component": "Frame"}], forModel=ref, forNode="turret")
        assert not decorating.errors, decorating.errors
        found = await schema.execute("query($ref: String!) { bloksForModel(ref: $ref) { name forNode context componentTree } }", context_value=authenticated_context, variable_values={"ref": ref})
        assert not found.errors, found.errors
        assert [(blok["name"], blok["forNode"], blok["context"]) for blok in found.data["bloksForModel"]] == [("catalogued", "turret", "PANEL")]
        root = found.data["bloksForModel"][0]["componentTree"][0]
        assert root["component"] == "Blok"
        assert [child["component"] for child in root["children"]] == ["local", "Frame"]

    async def test_a_custom_blok_is_no_logo_but_may_draw_one(self, authenticated_context: HttpContext) -> None:
        """A logo is an agent's (or an action's): a custom blok is drawn in a panel or a scene, and a panel may hold a Logo."""
        await _register(authenticated_context, {**CATALOG, "components": [{"name": "Frame"}]})
        logo = {"id": "logo", "component": "Logo", "children": [{"id": "g", "component": "G", "children": [{"id": "p", "component": "Path"}]}]}

        refused = await _create_blok(authenticated_context, [logo], context="LOGO")
        assert refused.errors and "A custom blok is drawn in panel, scene, not logo" in refused.errors[0].message, refused.errors
        embedded = await _create_blok(authenticated_context, [{"id": "root", "component": "Frame", "children": [logo]}])
        assert await _stored(embedded) == []
        # Where the catalog places a shape is a finding.
        loose = await _create_blok(authenticated_context, [{"id": "root", "component": "Frame", "children": [{"id": "p", "component": "Path"}]}])
        _unchecked(await _stored(loose))

    async def test_a_blok_may_be_sent_as_its_bsx_source(self, authenticated_context: HttpContext, takt: str) -> None:
        """takt parses the source into the tree a client would have sent: same tree, same hash."""
        from facade.models import Blok

        await _register(authenticated_context, {**CATALOG, "components": [{"name": "Frame"}, {"name": "Caption", "props": [{"key": "text", "kind": "STRING"}], "acceptsChildren": False}]})
        tree = _tree(node("Frame", node("Caption", text="hi")))
        opening = '<Blok kind="custom" context="panel"><local name="exposure" kind="INT" default={1} />'

        async def stored() -> tuple[list, str]:
            blok = await sync_to_async(lambda: Blok.objects.get(name="catalogued"))()
            result = await schema.execute("query($id: ID!) { blok(id: $id) { hash } }", context_value=authenticated_context, variable_values={"id": str(blok.id)})
            return blok.components, result.data["blok"]["hash"]

        as_tree = await _create_blok(authenticated_context, [], components=tree)
        assert not as_tree.errors, as_tree.errors
        from_tree = await stored()

        source = {"name": "catalogued", "vocab": 2, "module": "electron"}
        as_source = await schema.execute(CREATE_BLOK, context_value=authenticated_context, variable_values={"input": {**source, "source": f'{opening}<Frame><Caption text="hi" /></Frame></Blok>'}})
        assert not as_source.errors, as_source.errors
        assert await stored() == from_tree

        both = await schema.execute(CREATE_BLOK, context_value=authenticated_context, variable_values={"input": {**source, "components": tree, "source": f"{opening}<Frame /></Blok>"}})
        assert both.errors and "not both" in both.errors[0].message
        broken = await schema.execute(CREATE_BLOK, context_value=authenticated_context, variable_values={"input": {**source, "source": f"{opening}<Frame>\n<Caption>\n</Frame></Blok>"}})
        assert broken.errors and "line 3" in broken.errors[0].message, broken.errors
        # What a parsed source draws is held to the catalog like any tree.
        unknown = await schema.execute(CREATE_BLOK, context_value=authenticated_context, variable_values={"input": {**source, "source": f"{opening}<Frame><Knob /></Frame></Blok>"}})
        _unchecked(await _stored(unknown))

    async def test_a_blok_hash_follows_what_it_renders(self, authenticated_context: HttpContext) -> None:
        """The hash moves with the tree (its declarations included) and what the blok says beside it, and with nothing else."""
        from facade.models import Blok

        await _register(authenticated_context, {**CATALOG, "components": [{"name": "Frame"}]})
        query = "query($id: ID!) { blok(id: $id) { hash } }"

        async def hash_of(content: list[dict], **extra: object) -> str:
            created = await _create_blok(authenticated_context, content, **extra)
            assert not created.errors, created.errors
            blok_id = await sync_to_async(lambda: Blok.objects.get(name="catalogued").id)()
            result = await schema.execute(query, context_value=authenticated_context, variable_values={"id": str(blok_id)})
            assert not result.errors, result.errors
            return result.data["blok"]["hash"]

        frame = [{"id": "root", "component": "Frame"}]
        first = await hash_of(frame, description="one")
        assert len(first) == 64
        assert await hash_of(frame, description="described differently") == first
        assert await hash_of([{"id": "other", "component": "Frame"}]) != first
        # A local is a node of the tree: its default is part of what the blok draws.
        assert await hash_of([], components=_tree(*frame, declarations=(local("exposure", kind="INT", default=2),))) != first
        assert await hash_of(frame, demo={"args": {"x": 1}}) != first
        assert await hash_of(frame, catalogs=["ui@1"]) != first

    async def test_control_flow_takes_the_context_it_is_placed_in(self, authenticated_context: HttpContext) -> None:
        """The language's components (foreach) sit in a scene or a panel, and their children stay in that context."""
        await _register(authenticated_context, {**CATALOG, "components": [{"name": "Frame"}]})

        def loop(*children: dict, id: str = "loop") -> dict:
            return {
                "id": id,
                "component": "foreach",
                "props": [{"key": "items", "dynamicValue": {"path": "args.items"}}, {"key": "let", "staticValue": "item", "declaresValue": "item"}, {"key": "key", "dynamicValue": {"path": "item"}}],
                "children": list(children),
            }

        model = {"id": "m", "component": "Model", "props": [{"key": "src", "dynamicValue": {"path": "deps.robot.assets.arm"}}]}
        for roots, context in (
            ([{"id": "root", "component": "Scene", "children": [loop(model)]}], "SCENE"),
            ([loop({"id": "b", "component": "Frame"})], "PANEL"),
        ):
            accepted = await _create_blok(authenticated_context, roots, context=context, dependencies=[{"key": "robot"}])
            assert await _stored(accepted) == [], context

        # What the loop holds is still checked in the context it passes on.
        boxed = await _create_blok(authenticated_context, [{"id": "root", "component": "Scene", "children": [loop({"id": "b", "component": "Frame"})]}], context="SCENE")
        _unchecked(await _stored(boxed))
        # A scene blok holds one Scene, not a loop standing in for one.
        rooted = await _create_blok(authenticated_context, [loop({"id": "root", "component": "Scene"})], context="SCENE")
        assert rooted.errors and "A scene blok holds one <Scene> or one <Space> beside its declarations, not <foreach>" in rooted.errors[0].message

        # The base catalog of port definitions (base@1) is unchanged.
        listed = await schema.execute("query { baseCatalog { components { name inheritsContext } } }", context_value=authenticated_context)
        assert not listed.errors, listed.errors
        assert all(component["inheritsContext"] for component in listed.data["baseCatalog"]["components"])
        assert "foreach" in {component["name"] for component in listed.data["baseCatalog"]["components"]}

    async def test_registering_a_base_component_is_rejected(self, authenticated_context: HttpContext) -> None:
        """A UI catalog cannot redefine a base component."""
        result = await schema.execute(REGISTER_UI_CATALOG, context_value=authenticated_context, variable_values={"input": {**CATALOG, "components": [{"name": "match"}]}})
        assert result.errors and "cannot redefine base components ['match']" in result.errors[0].message

    async def test_unregistered_catalog_still_validates_the_language(self, authenticated_context: HttpContext) -> None:
        """A catalog that only exists because a blok named it checks no components, but the language still applies."""
        result = await _create_blok(authenticated_context, [{"id": "root", "component": "Whatever", "props": [{"key": "x", "utilCall": {"operation": "fmt.anything"}}]}], catalog="fresh")
        assert not result.errors, result.errors
        # Neither the module nobody registered nor a function of a namespace it may add is the server's to judge.
        assert [d["code"] for d in await _blok_diagnostics()] == []

        wrong_keys = await _create_blok(authenticated_context, [{"id": "root", "component": "Whatever", "props": [{"key": "x", "utilCall": {"operation": "cmp.gt", "arguments": [{"key": "left", "valueLiteral": 1}]}}]}], catalog="fresh")
        assert wrong_keys.errors and "has no argument 'left'" in wrong_keys.errors[0].message, wrong_keys.errors

    async def test_registering_a_base_name_is_rejected(self, authenticated_context: HttpContext) -> None:
        """A UI catalog cannot redefine a base operation."""
        result = await schema.execute(
            REGISTER_UI_CATALOG,
            context_value=authenticated_context,
            variable_values={"input": {**CATALOG, "operations": [{"name": "gt", "arguments": [], "returns": "BOOL"}]}},
        )
        assert result.errors and "cannot redefine base operations ['gt']" in result.errors[0].message
        assert await sync_to_async(Module.objects.filter(name="electron").count)() == 0

    async def test_base_catalog_query(self, authenticated_context: HttpContext) -> None:
        """The base catalog is queryable and identical for every organization."""
        result = await schema.execute(BASE_CATALOG, context_value=authenticated_context)
        assert not result.errors, result.errors
        base = result.data["baseCatalog"]
        assert base["name"] == "base" and base["version"] == 1
        gt = next(op for op in base["operations"] if op["name"] == "gt")
        assert gt["returns"] == "BOOL"
        assert [a["key"] for a in gt["arguments"]] == ["a", "b"]
        len_between = next(op for op in base["operations"] if op["name"] == "len_between")
        assert [(a["key"], a["required"]) for a in len_between["arguments"]] == [("value", True), ("min", True), ("max", False)]

        other, _, _, _ = await sync_to_async(tenant_context)(OTHER_TOKEN)
        theirs = await schema.execute(BASE_CATALOG, context_value=other)
        assert theirs.data == result.data


REGISTER_WITH_DEFAULTS = """
    mutation RegisterModule($input: RegisterModuleInput!) {
        registerModule(input: $input) {
            id
            widgetDefaults {
                kind
                identifier
                widget { kind ... on CustomAssignWidget { component props { key dynamicValue { path } } } ... on SliderAssignWidget { min max } }
                returnWidget { kind ... on CustomReturnWidget { component } }
            }
        }
    }
"""

DEFAULTS = [
    {"identifier": "@mikro/image", "widget": {"kind": "CUSTOM", "component": "Dial", "props": [{"key": "value", "dynamicValue": {"path": "/value"}}]}, "returnWidget": {"kind": "CUSTOM", "component": "Frame"}},
    {"kind": "FLOAT", "widget": {"kind": "SLIDER", "min": 0, "max": 1}},
]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestWidgetDefaults:
    async def test_register_and_query_widget_defaults(self, authenticated_context: HttpContext) -> None:
        """Defaults are stored typed and come back as the widget union."""
        result = await schema.execute(REGISTER_WITH_DEFAULTS, context_value=authenticated_context, variable_values={"input": {**CATALOG, "widgetDefaults": DEFAULTS}})
        assert not result.errors, result.errors

        by_image, by_float = result.data["registerModule"]["widgetDefaults"]
        assert by_image["identifier"] == "@mikro/image" and by_image["kind"] is None
        assert by_image["widget"] == {"kind": "CUSTOM", "component": "Dial", "props": [{"key": "value", "dynamicValue": {"path": "/value"}}]}
        assert by_image["returnWidget"] == {"kind": "CUSTOM", "component": "Frame"}
        assert by_float["kind"] == "FLOAT" and by_float["widget"] == {"kind": "SLIDER", "min": 0, "max": 1}

        stored = await sync_to_async(lambda: Module.objects.get(name="electron").widget_defaults)()
        assert [d["identifier"] for d in stored] == ["@mikro/image", None]

    async def test_default_naming_an_unregistered_component_is_rejected(self, authenticated_context: HttpContext) -> None:
        """A UI cannot announce a default it cannot render; nothing is written."""
        bad = [{"identifier": "@mikro/image", "widget": {"kind": "CUSTOM", "component": "Knob"}}]
        result = await schema.execute(REGISTER_WITH_DEFAULTS, context_value=authenticated_context, variable_values={"input": {**CATALOG, "widgetDefaults": bad}})
        assert result.errors and "component 'Knob' is not registered" in result.errors[0].message
        assert await sync_to_async(Module.objects.filter(name="electron").count)() == 0

    async def test_default_with_unknown_operation_is_rejected(self, authenticated_context: HttpContext) -> None:
        """Unknown operations are warnings on definitions but errors on a catalog's own defaults."""
        bad = [{"kind": "FLOAT", "widget": {"kind": "CUSTOM", "component": "Dial", "props": [{"key": "value", "staticValue": 1}, {"key": "onChange", "utilCall": {"operation": "fizz"}}]}}]
        result = await schema.execute(REGISTER_WITH_DEFAULTS, context_value=authenticated_context, variable_values={"input": {**CATALOG, "widgetDefaults": bad}})
        assert result.errors and "operation 'fizz' is not provided" in result.errors[0].message

    async def test_duplicate_selectors_are_rejected(self, authenticated_context: HttpContext) -> None:
        """Duplicate selectors are rejected."""
        dup = [{"kind": "FLOAT", "widget": {"kind": "SLIDER"}}, {"kind": "FLOAT", "widget": {"kind": "STRING"}}]
        result = await schema.execute(REGISTER_WITH_DEFAULTS, context_value=authenticated_context, variable_values={"input": {**CATALOG, "widgetDefaults": dup}})
        assert result.errors and "duplicate selector" in result.errors[0].message

    async def test_merged_widget_input_rejects_fields_of_another_kind(self, authenticated_context: HttpContext) -> None:
        """The merged AssignWidgetInput dispatches on kind and names a contradicting field instead of dropping it."""
        bad = [{"kind": "FLOAT", "widget": {"kind": "SLIDER", "component": "Frame"}}]
        result = await schema.execute(REGISTER_WITH_DEFAULTS, context_value=authenticated_context, variable_values={"input": {**CATALOG, "widgetDefaults": bad}})
        assert result.errors and "A SLIDER assign widget does not read `component`" in result.errors[0].message


CREATE_RESOLVED_BLOK = """
    mutation CreateBlok($input: CreateBlokInput!) {
        createBlok(input: $input) {
            id
            hash
            resolvedLocals { node key kind identifier nullable default }
            dependencies { key }
            diagnostics { code }
        }
    }
"""

MOVE_DEMAND = {
    "key": "stage",
    "actionDependencies": [{"key": "move_to", "demand": {"argMatches": [{"key": "image", "kind": "STRUCTURE", "identifier": "@mikro/image", "nullable": True}]}}],
}


def _move_button(path: str) -> dict:
    return {"id": "go", "component": "Button", "props": [{"key": "onClick", "agentCall": {"dependency": "stage", "operation": "move_to", "arguments": [{"key": "image", "valuePath": path}]}}]}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestResolvedBloks:
    """A blok's ``<local>`` declarations are stored as its tree resolves them (``resolvedLocals``,
    each with its node and a kind), recomputed on every write. Dependencies are never inferred."""

    async def _create(self, http: HttpContext, *content: dict, declarations: tuple[dict, ...] = (), **input: object) -> dict:
        result = await schema.execute(CREATE_RESOLVED_BLOK, context_value=http, variable_values={"input": {"name": "resolved", "vocab": 2, "components": _tree(*content, declarations=declarations), **input}})
        assert not result.errors, result.errors
        return result.data["createBlok"]

    async def test_a_left_out_kind_is_resolved_per_local_node(self, authenticated_context: HttpContext) -> None:
        """A local passed whole to a STRUCTURE argument takes that structure; one with only a default takes its kind. Each is told by its node."""
        blok = await self._create(authenticated_context, _move_button("picked"), declarations=(local("picked"), local("counted", default=2)), dependencies=[MOVE_DEMAND])
        assert blok["resolvedLocals"] == [
            {"node": "Blok[0]/local[0]", "key": "picked", "kind": "STRUCTURE", "identifier": "@mikro/image", "nullable": True, "default": None},
            {"node": "Blok[0]/local[1]", "key": "counted", "kind": "INT", "identifier": None, "nullable": False, "default": 2},
        ]

    async def test_a_custom_blok_must_declare_the_dependencies_its_tree_reads(self, authenticated_context: HttpContext) -> None:
        tree = {"id": "root", "component": "Card", "children": [{"id": "t", "component": "Text", "props": [{"key": "text", "dynamicValue": {"path": "deps.robot.arm.mode"}}]}, _move_button("picked")]}
        components = _tree(tree, declarations=(local("picked"),))
        result = await schema.execute(CREATE_RESOLVED_BLOK, context_value=authenticated_context, variable_values={"input": {"name": "resolved", "vocab": 2, "components": components, "dependencies": [MOVE_DEMAND]}})
        assert result.errors
        message = result.errors[0].message
        assert "'robot' is not a dependency of the blok" in message and 'Fix: dependencies=[AgentDependencyInput(key="robot")]' in message

    async def test_a_module_shadows_a_standard_component(self, authenticated_context: HttpContext) -> None:
        """What a module registers is stored for the frontends; the server checks no blok against it."""
        await _register(authenticated_context, {"name": "mine", "components": [{"name": "Slider", "props": [{"key": "bind", "kind": "STRING"}], "acceptsChildren": False}]})
        slider = {"id": "s", "component": "Slider", "props": [{"key": "bind", "dynamicValue": {"path": "label"}}]}
        blok = await self._create(authenticated_context, slider, declarations=(local("label", kind="STRING"),), module="mine")
        assert [diagnostic["code"] for diagnostic in blok["diagnostics"]] == []

    async def test_a_module_nobody_registered_is_not_the_servers_to_judge(self, authenticated_context: HttpContext) -> None:
        blok = await self._create(authenticated_context, {"id": "root", "component": "Card"}, module="nowhere")
        assert [diagnostic["code"] for diagnostic in blok["diagnostics"]] == []
