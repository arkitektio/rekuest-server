"""UI catalog registry: registration, tenant isolation, and validation of bloks against it."""

import itertools

import pytest
from asgiref.sync import sync_to_async
from kante.context import HttpContext

from facade.models import UICatalog
from facade.schema import schema
from tests.graphql.test_cross_tenant_isolation import OTHER_TOKEN, tenant_context
from tests.graphql_ops import CREATE_BLOK

REGISTER_UI_CATALOG = """
    mutation RegisterUiCatalog($input: RegisterUiCatalogInput!) {
        registerUiCatalog(input: $input) {
            id
            name
            description
            isRegistered
            components { name props { key kind required } acceptsChildren }
            operations { name arguments { key kind required } returns }
        }
    }
"""

UI_CATALOGS = "query { uiCatalogs { id name } }"
UI_CATALOG = "query UiCatalog($id: ID!) { uiCatalog(id: $id) { id name } }"

CATALOG = {
    "name": "electron",
    "description": "The desktop app",
    "components": [
        {"name": "Slider", "props": [{"key": "value", "kind": "FLOAT", "required": True}, {"key": "onChange", "kind": "CALLBACK"}], "acceptsChildren": False},
        {"name": "Box"},
    ],
    "operations": [
        {"name": "clamp", "arguments": [{"key": "a", "kind": "FLOAT"}, {"key": "b", "kind": "FLOAT"}], "returns": "FLOAT"},
        {"name": "fmt", "arguments": [{"key": "v", "kind": "ANY"}], "returns": "STRING"},
    ],
}

BASE_CATALOG = "query { baseCatalog { name version operations { name returns arguments { key kind required } } } }"


async def _blok_diagnostics(name: str = "catalogued") -> list[dict]:
    from facade.models import Blok

    return await sync_to_async(lambda: Blok.objects.get(name=name).diagnostics)()


async def _register(context: HttpContext, catalog: dict = CATALOG) -> dict:
    result = await schema.execute(REGISTER_UI_CATALOG, context_value=context, variable_values={"input": catalog})
    assert not result.errors, result.errors
    return result.data["registerUiCatalog"]


async def _create_blok(http: HttpContext, components: list[dict], catalog: str = "electron", **extra: object):
    return await schema.execute(
        CREATE_BLOK,
        context_value=http,
        variable_values={"input": {"name": "catalogued", "catalog": catalog, "components": components, "demoState": {"exposure": 1}, **extra}},
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestUiCatalog:
    async def test_register_upserts_by_name_within_the_organization(self, authenticated_context: HttpContext) -> None:
        """Registering twice under one name updates the same row and replaces its contents."""
        first = await _register(authenticated_context)
        assert first["isRegistered"] is True
        assert {c["name"] for c in first["components"]} == {"Slider", "Box"}
        assert first["components"][0]["props"][0] == {"key": "value", "kind": "FLOAT", "required": True}
        assert first["operations"][0]["returns"] == "FLOAT"

        second = await _register(authenticated_context, {**CATALOG, "components": [{"name": "Text"}], "operations": []})
        assert second["id"] == first["id"]
        assert [c["name"] for c in second["components"]] == ["Text"]
        assert second["operations"] == []
        assert await sync_to_async(UICatalog.objects.filter(name="electron").count)() == 1

    async def test_catalogs_are_scoped_to_the_organization(self, authenticated_context: HttpContext) -> None:
        """Another organization may register the same name and neither sees the other's catalog."""
        other, _, _, _ = await sync_to_async(tenant_context)(OTHER_TOKEN)

        mine = await _register(authenticated_context)
        theirs = await _register(other)
        assert mine["id"] != theirs["id"]

        listed = await schema.execute(UI_CATALOGS, context_value=authenticated_context)
        assert not listed.errors, listed.errors
        assert {row["id"] for row in listed.data["uiCatalogs"]} == {mine["id"]}

        foreign = await schema.execute(UI_CATALOG, context_value=authenticated_context, variable_values={"id": theirs["id"]})
        assert foreign.errors, "org A read org B's catalog"

    async def test_blok_against_registered_catalog_is_validated(self, authenticated_context: HttpContext) -> None:
        """Unknown components, props and operations are rejected; a conforming manifest is accepted."""
        await _register(authenticated_context)

        unknown_component = await _create_blok(authenticated_context, [{"id": "root", "component": "Knob"}])
        assert unknown_component.errors and "component 'Knob' is not registered" in unknown_component.errors[0].message

        unknown_prop = await _create_blok(authenticated_context, [{"id": "root", "component": "Slider", "props": [{"key": "value", "staticValue": 1}, {"key": "colour", "staticValue": "red"}]}])
        assert unknown_prop.errors and "has no props ['colour']" in unknown_prop.errors[0].message

        missing_required = await _create_blok(authenticated_context, [{"id": "root", "component": "Slider"}])
        assert missing_required.errors and "requires props ['value']" in missing_required.errors[0].message

        children_refused = await _create_blok(authenticated_context, [{"id": "root", "component": "Slider", "props": [{"key": "value", "staticValue": 1}], "children": [{"id": "c", "component": "Box"}]}])
        assert children_refused.errors and "does not accept children" in children_refused.errors[0].message

        unknown_operation = await _create_blok(
            authenticated_context,
            [{"id": "root", "component": "Slider", "props": [{"key": "value", "utilCall": {"operation": "fizz", "arguments": [{"key": "v", "valuePath": "/exposure"}]}}]}],
        )
        assert not unknown_operation.errors, unknown_operation.errors
        assert [d["code"] for d in await _blok_diagnostics()] == ["unknown_operation"]

        wrong_arguments = await _create_blok(
            authenticated_context,
            [{"id": "root", "component": "Slider", "props": [{"key": "value", "utilCall": {"operation": "gt", "arguments": [{"key": "a", "valuePath": "/exposure"}]}}]}],
        )
        assert wrong_arguments.errors and "requires arguments ['b']" in wrong_arguments.errors[0].message

        accepted = await _create_blok(
            authenticated_context,
            [
                {
                    "id": "root",
                    "component": "Box",
                    "children": [
                        {
                            "id": "slider",
                            "component": "Slider",
                            "props": [
                                {"key": "value", "utilCall": {"operation": "gt", "arguments": [{"key": "a", "valuePath": "/exposure"}, {"key": "b", "valueLiteral": 0}]}},
                                {"key": "onChange", "utilCall": {"operation": "fmt", "arguments": [{"key": "v", "valuePath": "/exposure"}]}},
                            ],
                        }
                    ],
                }
            ],
        )
        assert not accepted.errors, accepted.errors
        assert await _blok_diagnostics() == []

    async def test_a_blok_naming_no_catalog_is_checked_against_the_standard_one(self, authenticated_context: HttpContext) -> None:
        """Without a catalog name a blok is held to ``orkestrator``, the name the client checks against offline."""
        await _register(authenticated_context, {**CATALOG, "name": "orkestrator"})
        unnamed = {"name": "catalogued", "demoState": {"exposure": 1}}

        refused = await schema.execute(CREATE_BLOK, context_value=authenticated_context, variable_values={"input": {**unnamed, "components": [{"id": "root", "component": "Knob"}]}})
        assert refused.errors and "component 'Knob' is not registered" in refused.errors[0].message

        accepted = await schema.execute(CREATE_BLOK, context_value=authenticated_context, variable_values={"input": {**unnamed, "components": [{"id": "root", "component": "Box"}]}})
        assert not accepted.errors, accepted.errors

    async def test_components_are_held_to_their_declared_placement(self, authenticated_context: HttpContext) -> None:
        """A component is refused outside its context, without a required ancestor, or under a forbidden one."""
        scene = [
            {"name": "Box"},
            {"name": "Label", "acceptsChildren": False},
            {"name": "Scene", "childrenContext": "SCENE", "notDownstreamOf": ["Scene"]},
            {"name": "Model", "context": "SCENE", "downstreamOf": ["Scene"]},
            {"name": "Move", "context": "SCENE", "downstreamOf": ["Model", "Robot"], "acceptsChildren": False},
            {"name": "Popout", "context": "SCENE", "childrenContext": "HTML", "downstreamOf": ["Model", "Robot"]},
        ]
        await _register(authenticated_context, {**CATALOG, "components": scene})

        ids = itertools.count()

        def node(component: str, *children: dict) -> dict:
            return {"id": f"{component}{next(ids)}", "component": component, "children": list(children)}

        accepted = await _create_blok(authenticated_context, [node("Box", node("Scene", node("Model", node("Move"), node("Popout", node("Box", node("Label"))))))])
        assert not accepted.errors, accepted.errors

        for tree, message in (
            (node("Model"), "component 'Model' is a scene component and cannot be placed in the html context"),
            (node("Scene", node("Label")), "component 'Label' is a html component and cannot be placed in the scene context"),
            (node("Scene", node("Move")), "component 'Move' must be inside one of ['Model', 'Robot']"),
            (node("Scene", node("Model", node("Popout", node("Scene")))), "component 'Scene' must not be inside ['Scene']"),
        ):
            refused = await _create_blok(authenticated_context, [tree])
            assert refused.errors and message in refused.errors[0].message, refused.errors

    async def test_a_scene_blok_opens_a_scene_and_names_its_models_properly(self, authenticated_context: HttpContext) -> None:
        """The root of a scene blok opens a scene; a model names an asset of a declared dependency."""
        await _register(
            authenticated_context,
            {
                **CATALOG,
                "components": [
                    {"name": "Box"},
                    {"name": "Scene", "childrenContext": "SCENE", "notDownstreamOf": ["Scene"]},
                    {"name": "Model", "context": "SCENE", "downstreamOf": ["Scene"], "props": [{"key": "src", "kind": "ASSET", "required": True}]},
                ],
            },
        )

        def scene(src: dict) -> list[dict]:
            return [{"id": "scene", "component": "Scene", "children": [{"id": "model", "component": "Model", "props": [{"key": "src", **src}]}]}]

        named = {"dynamicValue": {"path": "self.assets.scope"}}
        own = [{"key": "self"}]

        ref = "sha256:" + "9f" * 32
        accepted = await _create_blok(authenticated_context, scene(named), context="SCENE", dependencies=own)
        assert not accepted.errors, accepted.errors
        from facade.models import Blok

        assert await sync_to_async(lambda: Blok.objects.get(name="catalogued").context)() == "SCENE"

        boxed = await _create_blok(authenticated_context, [{"id": "root", "component": "Box"}], context="SCENE")
        assert boxed.errors and "the root of a scene blok must be a 'Scene', not 'Box'" in boxed.errors[0].message
        wrapped = await _create_blok(authenticated_context, [{"id": "root", "component": "Box", "children": scene(named)}], context="SCENE", dependencies=own)
        assert wrapped.errors and "the root of a scene blok must be a 'Scene', not 'Box'" in wrapped.errors[0].message
        # No component is registered in this catalog, so nothing else could tell a scene from a box.
        uncatalogued = await _create_blok(authenticated_context, [{"id": "root", "component": "Box"}], catalog="unregistered", context="SCENE")
        assert uncatalogued.errors and "the root of a scene blok must be a 'Scene', not 'Box'" in uncatalogued.errors[0].message
        twice = scene(named) + [{"id": "other", "component": "Scene"}]
        for roots, count in ((twice, 2), ([], 0)):
            refused = await _create_blok(authenticated_context, roots, context="SCENE", dependencies=own)
            assert refused.errors and f"a scene blok has exactly one root, a 'Scene', not {count} roots" in refused.errors[0].message, refused.errors
        by_hash = await _create_blok(authenticated_context, scene({"staticValue": ref}), context="SCENE")
        assert by_hash.errors and "is an ASSET prop and must name an asset" in by_hash.errors[0].message
        misspelt = await _create_blok(authenticated_context, scene({"dynamicValue": {"path": "self.assets"}}), context="SCENE", dependencies=own)
        assert misspelt.errors and "is an ASSET prop and must name an asset" in misspelt.errors[0].message
        of_nobody = await _create_blok(authenticated_context, scene(named), context="SCENE")
        assert of_nobody.errors and "references 'self' but it is neither a demo_state key, a declared value nor a dependency key" in of_nobody.errors[0].message

        decorating_scene = await _create_blok(authenticated_context, scene(named), context="SCENE", dependencies=own, forModel=ref)
        assert decorating_scene.errors and "only an html blok can decorate a model" in decorating_scene.errors[0].message
        decorating = await _create_blok(authenticated_context, [{"id": "root", "component": "Box"}], forModel=ref, forNode="turret")
        assert not decorating.errors, decorating.errors
        found = await schema.execute("query($ref: String!) { bloksForModel(ref: $ref) { name forNode context componentTree } }", context_value=authenticated_context, variable_values={"ref": ref})
        assert not found.errors, found.errors
        assert [(blok["name"], blok["forNode"], blok["context"]) for blok in found.data["bloksForModel"]] == [("catalogued", "turret", "HTML")]
        assert found.data["bloksForModel"][0]["componentTree"][0]["component"] == "Box"

    async def test_a_logo_blok_is_one_svg(self, authenticated_context: HttpContext) -> None:
        """The root of a logo blok is an ``SVG``; the shapes it draws stay inside one."""
        await _register(
            authenticated_context,
            {
                **CATALOG,
                "components": [
                    {"name": "Box"},
                    {"name": "SVG", "childrenContext": "LOGO", "notDownstreamOf": ["SVG"]},
                    {"name": "G", "context": "LOGO", "downstreamOf": ["SVG"]},
                    {"name": "Path", "context": "LOGO", "downstreamOf": ["SVG"], "acceptsChildren": False},
                ],
            },
        )
        logo = [{"id": "logo", "component": "SVG", "children": [{"id": "g", "component": "G", "children": [{"id": "p", "component": "Path"}]}]}]

        accepted = await _create_blok(authenticated_context, logo, context="LOGO")
        assert not accepted.errors, accepted.errors
        from facade.models import Blok

        assert await sync_to_async(lambda: Blok.objects.get(name="catalogued").context)() == "LOGO"
        # An html blok may draw an SVG too, anywhere it likes.
        embedded = await _create_blok(authenticated_context, [{"id": "root", "component": "Box", "children": logo}])
        assert not embedded.errors, embedded.errors

        for roots, context, message in (
            ([{"id": "p", "component": "Path"}], "LOGO", "the root of a logo blok must be a 'SVG', not 'Path'"),
            ([{"id": "root", "component": "Box", "children": logo}], "LOGO", "the root of a logo blok must be a 'SVG', not 'Box'"),
            (logo + [{"id": "other", "component": "SVG"}], "LOGO", "a logo blok has exactly one root, a 'SVG', not 2 roots"),
            ([{"id": "root", "component": "Box", "children": [{"id": "p", "component": "Path"}]}], "HTML", "component 'Path' is a logo component and cannot be placed in the html context"),
        ):
            refused = await _create_blok(authenticated_context, roots, context=context)
            assert refused.errors and message in refused.errors[0].message, refused.errors

    async def test_an_action_logo_is_one_action_logo_in_the_palette(self, authenticated_context: HttpContext) -> None:
        """An action logo's root is an ``ActionLogo``; it paints with the palette and reads ``task`` from its demo state."""
        await _register(
            authenticated_context,
            {
                **CATALOG,
                "components": [
                    {"name": "SVG", "childrenContext": "LOGO", "notDownstreamOf": ["SVG", "ActionLogo"]},
                    {"name": "ActionLogo", "childrenContext": "LOGO", "notDownstreamOf": ["SVG", "ActionLogo"]},
                    {"name": "Path", "context": "LOGO", "downstreamOf": ["SVG", "ActionLogo"], "acceptsChildren": False, "props": [{"key": "fill", "kind": "STRING", "required": False}]},
                ],
            },
        )

        def drawn(root: str, fill: dict) -> list[dict]:
            return [{"id": "logo", "component": root, "children": [{"id": "p", "component": "Path", "props": [{"key": "fill", **fill}]}]}]

        quiet = await _create_blok(authenticated_context, drawn("ActionLogo", {"dynamicValue": {"path": "task.tint"}}), context="ACTION_LOGO", demoState={"task": None})
        assert not quiet.errors, quiet.errors
        # A logo keeps any SVG colour.
        loud_logo = await _create_blok(authenticated_context, drawn("SVG", {"staticValue": "#fbbf24"}), context="LOGO")
        assert not loud_logo.errors, loud_logo.errors

        for components, message in (
            (drawn("ActionLogo", {"staticValue": "#fbbf24"}), "not '#fbbf24' in 'fill'"),
            (drawn("SVG", {"staticValue": "brand"}), "the root of a action_logo blok must be a 'ActionLogo', not 'SVG'"),
        ):
            refused = await _create_blok(authenticated_context, components, context="ACTION_LOGO", demoState={"task": None})
            assert refused.errors and message in refused.errors[0].message, refused.errors
        # No component lives in the action logo context: its shapes are logo components.
        placed = await schema.execute(
            REGISTER_UI_CATALOG,
            context_value=authenticated_context,
            variable_values={"input": {**CATALOG, "components": [{"name": "Path", "context": "ACTION_LOGO"}]}},
        )
        assert placed.errors and "ACTION_LOGO is a blok context only" in placed.errors[0].message, placed.errors

    async def test_a_blok_may_be_sent_as_its_bsx_source(self, authenticated_context: HttpContext, takt: str) -> None:
        """takt parses the source into the tree a client would have sent: same tree, same hash."""
        from facade.models import Blok

        await _register(authenticated_context, {**CATALOG, "components": [{"name": "Box"}, {"name": "Label", "props": [{"key": "text", "kind": "STRING"}], "acceptsChildren": False}]})
        tree = [{"id": "Box", "component": "Box", "children": [{"id": "Box/Label[0]", "component": "Label", "props": [{"key": "text", "staticValue": "hi"}]}]}]

        async def stored() -> tuple[list, str]:
            blok = await sync_to_async(lambda: Blok.objects.get(name="catalogued"))()
            result = await schema.execute("query($id: ID!) { blok(id: $id) { hash } }", context_value=authenticated_context, variable_values={"id": str(blok.id)})
            return blok.components, result.data["blok"]["hash"]

        as_tree = await _create_blok(authenticated_context, tree)
        assert not as_tree.errors, as_tree.errors
        from_tree = await stored()

        as_source = await schema.execute(CREATE_BLOK, context_value=authenticated_context, variable_values={"input": {"name": "catalogued", "catalog": "electron", "source": '<Box><Label text="hi" /></Box>', "demoState": {"exposure": 1}}})
        assert not as_source.errors, as_source.errors
        assert await stored() == from_tree

        both = await schema.execute(CREATE_BLOK, context_value=authenticated_context, variable_values={"input": {"name": "catalogued", "catalog": "electron", "components": tree, "source": "<Box />"}})
        assert both.errors and "not both" in both.errors[0].message
        broken = await schema.execute(CREATE_BLOK, context_value=authenticated_context, variable_values={"input": {"name": "catalogued", "catalog": "electron", "source": "<Box>\n<Label>\n</Box>"}})
        assert broken.errors and "line 3" in broken.errors[0].message, broken.errors
        # What a parsed source draws is held to the catalog like any tree.
        unknown = await schema.execute(CREATE_BLOK, context_value=authenticated_context, variable_values={"input": {"name": "catalogued", "catalog": "electron", "source": "<Box><Knob /></Box>"}})
        assert unknown.errors and "component 'Knob' is not registered" in unknown.errors[0].message

    async def test_a_blok_hash_follows_what_it_renders(self, authenticated_context: HttpContext) -> None:
        """The hash moves with the tree, the demo state and the context, and with nothing else."""
        from facade.models import Blok

        await _register(authenticated_context, {**CATALOG, "components": [{"name": "Box"}]})
        query = "query($id: ID!) { blok(id: $id) { hash } }"

        async def hash_of(components: list[dict], **extra: object) -> str:
            created = await _create_blok(authenticated_context, components, **extra)
            assert not created.errors, created.errors
            blok_id = await sync_to_async(lambda: Blok.objects.get(name="catalogued").id)()
            result = await schema.execute(query, context_value=authenticated_context, variable_values={"id": str(blok_id)})
            assert not result.errors, result.errors
            return result.data["blok"]["hash"]

        box = [{"id": "root", "component": "Box"}]
        first = await hash_of(box, description="one")
        assert len(first) == 64
        assert await hash_of(box, description="described differently") == first
        assert await hash_of([{"id": "other", "component": "Box"}]) != first
        assert await hash_of(box, demoState={"exposure": 2}) != first

    async def test_a_logo_is_held_to_the_size_of_an_icon(self, authenticated_context: HttpContext) -> None:
        """At most 200 components, 12 deep, 16 KB of path data, and 64 items per foreach of its demo state."""
        await _register(
            authenticated_context,
            {
                **CATALOG,
                "components": [
                    {"name": "SVG", "childrenContext": "LOGO", "notDownstreamOf": ["SVG"]},
                    {"name": "G", "context": "LOGO", "downstreamOf": ["SVG"]},
                    {"name": "Path", "context": "LOGO", "downstreamOf": ["SVG"], "acceptsChildren": False, "props": [{"key": "d", "kind": "STRING"}]},
                    {"name": "foreach", "inheritsContext": True, "props": [{"key": "items", "kind": "LIST"}]},
                ],
            },
        )

        def svg(*children: dict) -> list[dict]:
            return [{"id": "logo", "component": "SVG", "children": list(children)}]

        def path(i: int, d: str = "M0 0") -> dict:
            return {"id": f"p{i}", "component": "Path", "props": [{"key": "d", "staticValue": d}]}

        def nested(depth: int) -> dict:
            node = {"id": "g0", "component": "G"}
            for i in range(1, depth):
                node = {"id": f"g{i}", "component": "G", "children": [node]}
            return node

        loop = {"id": "loop", "component": "foreach", "props": [{"key": "items", "dynamicValue": {"path": "lamps"}}], "children": [path(0)]}

        fits = await _create_blok(authenticated_context, svg(*(path(i) for i in range(199))), context="LOGO")
        assert not fits.errors, fits.errors
        drawn = await _create_blok(authenticated_context, svg(loop), context="LOGO", demoState={"lamps": list(range(64))})
        assert not drawn.errors, drawn.errors

        for components, demo_state, message in (
            (svg(*(path(i) for i in range(200))), {}, "a logo has at most 200 components, this one 201"),
            (svg(nested(12)), {}, "a logo nests at most 12 deep"),
            (svg(path(0, "M" + "1" * 8192), path(1, "M" + "1" * 8192)), {}, "a logo holds at most 16384 characters of path data"),
            (svg(loop), {"lamps": list(range(65))}, "a foreach in a logo draws at most 64 items, its demo state has 65"),
        ):
            refused = await _create_blok(authenticated_context, components, context="LOGO", demoState=demo_state)
            assert refused.errors and message in refused.errors[0].message, refused.errors
        # A panel draws as much as it likes.
        panel = await _create_blok(authenticated_context, [{"id": "root", "component": "foreach", "children": svg(*(path(i) for i in range(300)))}])
        assert not panel.errors, panel.errors

    async def test_control_flow_takes_the_context_it_is_placed_in(self, authenticated_context: HttpContext) -> None:
        """A component that inherits its context sits in a scene or a logo, and its children stay there."""
        await _register(
            authenticated_context,
            {
                **CATALOG,
                "components": [
                    {"name": "Box"},
                    {"name": "foreach", "inheritsContext": True},
                    {"name": "Scene", "childrenContext": "SCENE", "notDownstreamOf": ["Scene"]},
                    {"name": "Model", "context": "SCENE", "downstreamOf": ["Scene"]},
                    {"name": "SVG", "childrenContext": "LOGO", "notDownstreamOf": ["SVG"]},
                    {"name": "Path", "context": "LOGO", "downstreamOf": ["SVG"], "acceptsChildren": False},
                ],
            },
        )

        def loop(*children: dict, id: str = "loop") -> dict:
            return {"id": id, "component": "foreach", "children": list(children)}

        for roots, context in (
            ([{"id": "root", "component": "SVG", "children": [loop({"id": "p", "component": "Path"})]}], "LOGO"),
            ([{"id": "root", "component": "Scene", "children": [loop({"id": "m", "component": "Model"})]}], "SCENE"),
            ([loop({"id": "b", "component": "Box"})], "HTML"),
        ):
            accepted = await _create_blok(authenticated_context, roots, context=context)
            assert not accepted.errors, accepted.errors

        # What the loop holds is still checked in the context it passes on.
        boxed = await _create_blok(authenticated_context, [{"id": "root", "component": "SVG", "children": [loop({"id": "b", "component": "Box"})]}], context="LOGO")
        assert boxed.errors and "component 'Box' is a html component and cannot be placed in the logo context" in boxed.errors[0].message
        # The context is the blok's, not something a loop at the root can stand in for.
        rooted = await _create_blok(authenticated_context, [loop({"id": "root", "component": "SVG"})], context="LOGO")
        assert rooted.errors and "the root of a logo blok must be a 'SVG', not 'foreach'" in rooted.errors[0].message

        listed = await schema.execute("query { uiCatalogs { name components { name inheritsContext } } }", context_value=authenticated_context)
        assert not listed.errors, listed.errors
        (electron,) = [catalog for catalog in listed.data["uiCatalogs"] if catalog["name"] == "electron"]
        assert {component["name"] for component in electron["components"] if component["inheritsContext"]} == {"foreach"}

    async def test_unregistered_catalog_still_validates_base(self, authenticated_context: HttpContext) -> None:
        """A catalog that only exists because a blok named it checks no components, but base operations still apply."""
        result = await _create_blok(authenticated_context, [{"id": "root", "component": "Whatever", "props": [{"key": "x", "utilCall": {"operation": "anything"}}]}], catalog="fresh")
        assert not result.errors, result.errors
        assert [d["code"] for d in await _blok_diagnostics()] == ["unknown_operation"]

        wrong_keys = await _create_blok(authenticated_context, [{"id": "root", "component": "Whatever", "props": [{"key": "x", "utilCall": {"operation": "gt", "arguments": [{"key": "left", "valueLiteral": 1}]}}]}], catalog="fresh")
        assert wrong_keys.errors and "does not accept arguments ['left']" in wrong_keys.errors[0].message

    async def test_registering_a_base_name_is_rejected(self, authenticated_context: HttpContext) -> None:
        """A UI catalog cannot redefine a base operation."""
        result = await schema.execute(
            REGISTER_UI_CATALOG,
            context_value=authenticated_context,
            variable_values={"input": {**CATALOG, "operations": [{"name": "gt", "arguments": [], "returns": "BOOL"}]}},
        )
        assert result.errors and "cannot redefine base operations ['gt']" in result.errors[0].message
        assert await sync_to_async(UICatalog.objects.filter(name="electron").count)() == 0

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
    mutation RegisterUiCatalog($input: RegisterUiCatalogInput!) {
        registerUiCatalog(input: $input) {
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
    {"identifier": "@mikro/image", "widget": {"kind": "CUSTOM", "component": "Slider", "props": [{"key": "value", "dynamicValue": {"path": "/value"}}]}, "returnWidget": {"kind": "CUSTOM", "component": "Box"}},
    {"kind": "FLOAT", "widget": {"kind": "SLIDER", "min": 0, "max": 1}},
]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestWidgetDefaults:
    async def test_register_and_query_widget_defaults(self, authenticated_context: HttpContext) -> None:
        """Defaults are stored typed and come back as the widget union."""
        result = await schema.execute(REGISTER_WITH_DEFAULTS, context_value=authenticated_context, variable_values={"input": {**CATALOG, "widgetDefaults": DEFAULTS}})
        assert not result.errors, result.errors

        by_image, by_float = result.data["registerUiCatalog"]["widgetDefaults"]
        assert by_image["identifier"] == "@mikro/image" and by_image["kind"] is None
        assert by_image["widget"] == {"kind": "CUSTOM", "component": "Slider", "props": [{"key": "value", "dynamicValue": {"path": "/value"}}]}
        assert by_image["returnWidget"] == {"kind": "CUSTOM", "component": "Box"}
        assert by_float["kind"] == "FLOAT" and by_float["widget"] == {"kind": "SLIDER", "min": 0, "max": 1}

        stored = await sync_to_async(lambda: UICatalog.objects.get(name="electron").widget_defaults)()
        assert [d["identifier"] for d in stored] == ["@mikro/image", None]

    async def test_default_naming_an_unregistered_component_is_rejected(self, authenticated_context: HttpContext) -> None:
        """A UI cannot announce a default it cannot render; nothing is written."""
        bad = [{"identifier": "@mikro/image", "widget": {"kind": "CUSTOM", "component": "Knob"}}]
        result = await schema.execute(REGISTER_WITH_DEFAULTS, context_value=authenticated_context, variable_values={"input": {**CATALOG, "widgetDefaults": bad}})
        assert result.errors and "component 'Knob' is not registered" in result.errors[0].message
        assert await sync_to_async(UICatalog.objects.filter(name="electron").count)() == 0

    async def test_default_with_unknown_operation_is_rejected(self, authenticated_context: HttpContext) -> None:
        """Unknown operations are warnings on definitions but errors on a catalog's own defaults."""
        bad = [{"kind": "FLOAT", "widget": {"kind": "CUSTOM", "component": "Slider", "props": [{"key": "value", "staticValue": 1}, {"key": "onChange", "utilCall": {"operation": "fizz"}}]}}]
        result = await schema.execute(REGISTER_WITH_DEFAULTS, context_value=authenticated_context, variable_values={"input": {**CATALOG, "widgetDefaults": bad}})
        assert result.errors and "operation 'fizz' is not provided" in result.errors[0].message

    async def test_duplicate_selectors_are_rejected(self, authenticated_context: HttpContext) -> None:
        """Duplicate selectors are rejected."""
        dup = [{"kind": "FLOAT", "widget": {"kind": "SLIDER"}}, {"kind": "FLOAT", "widget": {"kind": "STRING"}}]
        result = await schema.execute(REGISTER_WITH_DEFAULTS, context_value=authenticated_context, variable_values={"input": {**CATALOG, "widgetDefaults": dup}})
        assert result.errors and "duplicate selector" in result.errors[0].message

    async def test_merged_widget_input_rejects_fields_of_another_kind(self, authenticated_context: HttpContext) -> None:
        """The merged AssignWidgetInput dispatches on kind and names a contradicting field instead of dropping it."""
        bad = [{"kind": "FLOAT", "widget": {"kind": "SLIDER", "component": "Box"}}]
        result = await schema.execute(REGISTER_WITH_DEFAULTS, context_value=authenticated_context, variable_values={"input": {**CATALOG, "widgetDefaults": bad}})
        assert result.errors and "A SLIDER assign widget does not read `component`" in result.errors[0].message
