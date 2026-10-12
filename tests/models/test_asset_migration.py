"""Migration 0003 keeps stored UI catalogs readable: a model's file is named by an asset."""

import importlib

import pytest
from authentikate.models import Organization
from django.apps import apps

from facade.models import UICatalog
from rekuest_core.inputs.models import CatalogComponentInputModel

migration = importlib.import_module("facade.migrations.0003_assets")


@pytest.mark.django_db
def test_a_catalog_from_before_assets_names_models_by_asset() -> None:
    organization = Organization.objects.create(slug="catalog-org")
    components = [
        {"name": "Box", "props": []},
        {
            "name": "Model",
            "context": "SCENE",
            "props": [
                {"key": "from", "kind": "HTTPS_URL", "required": True},
                {"key": "id", "kind": "STRING", "required": False},
                {"key": "src", "kind": "MODEL_REF", "required": True},
            ],
        },
    ]
    catalog = UICatalog.objects.create(organization=organization, name="orkestrator", components=components, operations=[])
    untouched = UICatalog.objects.create(organization=organization, name="plain", components=[{"name": "Box", "props": []}], operations=[])

    migration.name_models_by_asset(apps, None)
    migration.name_models_by_asset(apps, None)

    catalog.refresh_from_db()
    model = CatalogComponentInputModel.model_validate(catalog.components[1])
    assert [(prop.key, prop.kind.value) for prop in model.props] == [("id", "STRING"), ("src", "ASSET")]
    untouched.refresh_from_db()
    assert untouched.components == [{"name": "Box", "props": []}]
