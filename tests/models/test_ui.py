"""UI-component model tests: Blok and Dashboard creation."""

import pytest

from facade.models import Blok, Dashboard


def _org(slug="test-org-scope"):
    """Protocol / StateDefinition / Blok / Dashboard are organization-scoped; any org will do here."""
    from authentikate.models import Organization

    return Organization.objects.get_or_create(slug=slug)[0]


def _catalog(org, name="default"):
    from facade.models import UICatalog

    return UICatalog.objects.get_or_create(name=name, organization=org)[0]


@pytest.mark.django_db(transaction=True)
class TestUIModels:
    """Test suite for the Blok / Dashboard UI models."""

    def test_blok_creation(self):
        """Test creating a Blok model instance."""
        # Create a test user for creator
        from django.contrib.auth import get_user_model

        User = get_user_model()
        user = User.objects.create(username="testuser", email="test@example.com")

        org = _org()
        blok = Blok.objects.create(name="Test Blok", description="A test UI blok", creator=user, organization=org, catalog=_catalog(org))

        assert blok.name == "Test Blok"
        assert blok.description == "A test UI blok"
        assert blok.creator == user
        assert blok.components == []
        assert blok.demo_state == {}

    def test_dashboard_creation(self):
        """Test creating a Dashboard model instance."""
        dashboard = Dashboard.objects.create(name="Test Dashboard", organization=_org())

        assert dashboard.name == "Test Dashboard"

    def test_blok_name_unique_per_organization(self):
        """The (organization, name) pair every write path upserts on is enforced by the database."""
        from django.contrib.auth import get_user_model
        from django.db import IntegrityError, transaction

        user = get_user_model().objects.create(username="unique-blok-user")
        org_a, org_b = _org("unique-a"), _org("unique-b")
        Blok.objects.create(name="Same", creator=user, organization=org_a, catalog=_catalog(org_a))

        with pytest.raises(IntegrityError), transaction.atomic():
            Blok.objects.create(name="Same", creator=user, organization=org_a, catalog=_catalog(org_a))

        # A different organization may reuse the name.
        Blok.objects.create(name="Same", creator=user, organization=org_b, catalog=_catalog(org_b))

    def test_dependency_key_unique_per_blok(self):
        """A blok cannot declare the same dependency key twice."""
        from django.contrib.auth import get_user_model
        from django.db import IntegrityError, transaction

        from facade.models import BlokDependency

        user = get_user_model().objects.create(username="unique-dep-user")
        org = _org("unique-dep")
        blok = Blok.objects.create(name="Deps", creator=user, organization=org, catalog=_catalog(org))
        BlokDependency.objects.create(blok=blok, key="stage")

        with pytest.raises(IntegrityError), transaction.atomic():
            BlokDependency.objects.create(blok=blok, key="stage")
