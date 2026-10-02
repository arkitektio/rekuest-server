import os
import time

import boto3
import psycopg
import pytest
from moto import mock_aws

from authentikate.models import Membership
from authentikate.expand import (
    aexpand_client_from_token,
    aexpand_organization_from_token,
    aexpand_user_from_token,
)
from authentikate.utils import authenticate_token_or_none
from django.conf import settings
from kante.context import HttpContext, UniversalRequest
from strawberry.http.temporal_response import TemporalResponse
from dokker import testing



@pytest.fixture(scope="session", autouse=True)
def embedding_model_warm():
    """Load the embedding model once per session, outside any test's DB transaction.

    Every save of an Action embeds its text, so the first one would otherwise pay the model
    load (a one-time download into the Hugging Face cache on a cold box) inside a test.
    """
    from embeddings import engine

    engine.warm_up()
    yield


@pytest.fixture(scope="function")
def aws_credentials() -> None:
    """Mocked AWS Credentials for moto."""
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"


@pytest.fixture(scope="function")
def s3(aws_credentials):
    with mock_aws():
        yield boto3.client("s3", region_name="us-east-1")


@pytest.fixture
def create_bucket1(s3) -> None:
    s3.create_bucket(Bucket="babanana")


@pytest.fixture
def create_bucket2(s3) -> None:
    s3.create_bucket(Bucket="cabanana")


@pytest.fixture(scope="session")
def backend_stack():
    docker_compose_path = os.path.join(os.path.dirname(__file__), "integration", "docker-compose.yaml")

    with testing(docker_compose_path) as e:
        e.inspect()

        e.down()

        e.up()

        deadline = time.monotonic() + 30
        while True:
            try:
                with psycopg.connect(
                    dbname="testdb",
                    user="test",
                    password="test",
                    host="localhost",
                    port=int(os.environ.get("REKUEST_TEST_DB_PORT", 5555)),
                    connect_timeout=1,
                ) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT 1")
                break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)

        yield


@pytest.fixture(scope="session")
def django_db_modify_db_settings(backend_stack):
    """Start the backend services before pytest-django configures the test DB.

    The published host ports are reserved per run (see the root ``conftest.py``),
    but Django settings are imported by pytest-django before that module ever
    executes, so ``settings_test`` will still be holding the defaults. This is
    the hook pytest-django provides for exactly that ordering: point the
    connection at the ports the stack actually came up on, before the test
    database is created.
    """
    settings.DATABASES["default"]["PORT"] = int(
        os.environ.get("REKUEST_TEST_DB_PORT", settings.DATABASES["default"]["PORT"])
    )
    settings.AGENT_REDIS_PORT = int(
        os.environ.get("REKUEST_TEST_REDIS_PORT", settings.AGENT_REDIS_PORT)
    )
    yield


@pytest.fixture(scope="function")
def authenticated_context(db, backend_stack):
    # Derive the identity from the same static token the agent sockets use, so a caller built
    # here and an agent from ``seed_agent`` land in the SAME organization. This used to
    # hardcode ``slug="test-organization"`` while ``seed_agent`` resolved the token's own org,
    # which silently put caller and agent in different tenants — invisible until the resolvers
    # started enforcing organization scoping.
    from asgiref.sync import async_to_sync

    from tests.factories import TEST_TOKEN

    decoded = async_to_sync(authenticate_token_or_none)(TEST_TOKEN)
    user = async_to_sync(aexpand_user_from_token)(decoded)
    client = async_to_sync(aexpand_client_from_token)(decoded)
    org = async_to_sync(aexpand_organization_from_token)(decoded)
    membership, _ = Membership.objects.get_or_create(
        user=user,
        organization=org,
    )

    request = UniversalRequest(
        _extensions={"token": "test"},
        _client=client,  # type: ignore
        _user=user,  # type: ignore
        _organization=org,  # type: ignore
    )
    request.set_membership(membership)  # type: ignore

    return HttpContext(request=request, response=TemporalResponse(), headers={"Authorization": "Bearer test"}, type="http")


@pytest.fixture
def fake_takt(monkeypatch, settings):
    """takt's internal API answered in-process (``tests/takt_fake.py``)."""
    from facade import takt
    from tests import takt_fake

    settings.TAKT_URL = "http://takt.test/rekuest"
    takt_fake.calls.clear()
    monkeypatch.setattr(takt, "call", takt_fake.call)
    return takt_fake
