"""Catalog ports: their shape, and the three kinds only a catalog types by."""

import pytest
from pydantic import ValidationError

from rekuest_core.inputs import models as imodels

CATALOG_ONLY = ("CALLBACK", "ASSET", "BINDING")


@pytest.mark.parametrize("kind", CATALOG_ONLY)
def test_no_agent_port_declares_a_catalog_only_kind(kind: str) -> None:
    """An action's argument or return (at any depth), a state, a local, a demand's match and a
    widget default are refused (``port.catalog_only``)."""
    item = {"key": "...", "kind": kind}
    attempts = [
        (imodels.ArgPortInputModel, {"key": "a", "kind": kind}),
        (imodels.ArgPortInputModel, {"key": "a", "kind": "LIST", "children": [item]}),
        (
            imodels.ReturnPortInputModel,
            {"key": "r", "kind": "LIST", "children": [item]},
        ),
        (
            imodels.StateDefinitionInputModel,
            {"name": "s", "ports": [{"key": "p", "kind": kind}]},
        ),
        (imodels.BlokLocalInputModel, {"key": "l", "kind": "LIST", "children": [item]}),
        (imodels.PortMatchInputModel, {"kind": kind}),
        (imodels.PortMatchInputModel, {"kind": "LIST", "children": [{"kind": kind}]}),
        (imodels.WidgetDefaultInputModel, {"kind": kind, "widget": {"kind": "STRING"}}),
    ]
    for model, data in attempts:
        with pytest.raises(ValidationError, match="port.catalog_only"):
            model.model_validate(data)
    with pytest.raises(ValidationError):  # not a LocalKind
        imodels.BlokLocalInputModel.model_validate({"key": "l", "kind": kind})


def test_a_catalog_port_takes_what_its_kind_needs() -> None:
    port = imodels.CatalogPortInputModel.model_validate
    port({"kind": "CALLBACK", "children": [{"key": "value", "kind": "BOOL"}]})
    port({"kind": "BINDING", "children": [{"key": "...", "kind": "STRING"}]})
    port({"kind": "STRUCTURE", "identifier": "*"})
    port({"kind": "LIST", "children": [{"key": "...", "kind": None}]})
    port(
        {
            "kind": "UNION",
            "children": [{"key": "0", "kind": "STRING"}, {"key": "0", "kind": "INT"}],
        }
    )
    port({"kind": "STRING", "choices": [{"value": "outline"}]})
    for bad in (
        {"kind": "ASSET", "children": [{"key": "x", "kind": "STRING"}]},
        {"kind": "BINDING", "children": [{"key": "value", "kind": "STRING"}]},
        {"kind": "STRUCTURE"},
        {"kind": "ENUM"},
        {"kind": "LIST", "children": []},
        {"kind": "UNION", "children": [{"key": "0", "kind": "STRING"}]},
        {
            "kind": "MODEL",
            "children": [{"key": "a", "kind": "INT"}, {"key": "a", "kind": "INT"}],
        },
        {"kind": "STRING", "values": ["a"]},
    ):
        with pytest.raises(ValidationError):
            port(bad)


def test_a_prop_typed_by_a_kind_is_refused() -> None:
    """A prop is typed by its port: a catalog written before ports does not parse."""
    with pytest.raises(ValidationError):
        imodels.CatalogPropInputModel.model_validate({"key": "label", "kind": "STRING"})
