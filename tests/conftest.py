import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable, Iterator
from typing import TYPE_CHECKING

import boto3
import httpx
import psycopg
import pytest
from authentikate.expand import (
    aexpand_client_from_token,
    aexpand_organization_from_token,
    aexpand_user_from_token,
)
from authentikate.models import Membership
from authentikate.utils import authenticate_token_or_none
from django.conf import settings
from dokker import Deployment, testing
from kante.context import HttpContext, UniversalRequest
from moto import mock_aws
from pytest_django.fixtures import SettingsWrapper
from strawberry.http.temporal_response import TemporalResponse

if TYPE_CHECKING:
    from facade import models


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


def _test_db(dbname: str) -> psycopg.Connection:
    return psycopg.connect(dbname=dbname, user="test", password="test", host="localhost", port=int(os.environ.get("REKUEST_TEST_DB_PORT", 5555)), connect_timeout=1, autocommit=True)


def _wait(what: str, ready: Callable[[], bool], seconds: float) -> None:
    """Poll ``ready`` until it is true; ``TimeoutError`` naming ``what`` after ``seconds``."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            if ready():
                return
        except (psycopg.OperationalError, httpx.HTTPError):
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{what} did not happen within {seconds:.0f} s")
        time.sleep(0.25)


@pytest.fixture(scope="session")
def backend_stack() -> Iterator[Deployment]:
    """Postgres and redis, up for the session. takt joins them later (``takt_stack``)."""
    docker_compose_path = os.path.join(os.path.dirname(__file__), "integration", "docker-compose.yaml")

    with testing(docker_compose_path) as e:
        e.inspect()

        e.down()

        e.up(services=["db", "redis"])

        def answers() -> bool:
            with _test_db("testdb") as connection:
                connection.execute("SELECT 1")
            return True

        _wait("postgres answering", answers, 30)
        yield e


@pytest.fixture(scope="session")
def takt_stack(backend_stack: Deployment, django_db_setup: None) -> Iterator[str]:
    """A real takt, working in the test database; the URL of its internal API.

    Started only now: takt needs the database to exist, and waits until it is migrated. Stopped
    before pytest-django drops that database again.
    """
    backend_stack.up(services=["takt"], build=not os.environ.get("TAKT_IMAGE"))
    url = f"http://localhost:{os.environ['REKUEST_TEST_TAKT_PORT']}"
    _wait("takt answering", lambda: httpx.get(f"{url}/ht", timeout=2).status_code == 200, 180)

    def listening() -> bool:
        with _test_db("test_testdb") as connection:
            return connection.execute("SELECT 1 FROM pg_stat_activity WHERE query ILIKE 'LISTEN%rekuest_schedule%'").fetchone() is not None

    # takt answers before its ear for schedule notices is in place.
    _wait("takt listening for schedule notices", listening, 30)
    yield url
    # When a test against takt fails, what takt logged says why: REKUEST_TEST_TAKT_LOG names a file for it.
    if os.environ.get("REKUEST_TEST_TAKT_LOG"):
        with open(os.environ["REKUEST_TEST_TAKT_LOG"], "w") as log:
            log.write("\n".join(str(line) for line in backend_stack.logs(services=["takt"], tail="400")))
    backend_stack.kill(services=["takt"])


@pytest.fixture
def takt(takt_stack: str, settings: SettingsWrapper) -> str:
    """This test talks to the real takt. Its rows must be committed: ``django_db(transaction=True)``."""
    settings.TAKT_URL = takt_stack
    return takt_stack


class ScheduleNotices:
    """What this server tells takt about its schedules: the notices on ``rekuest_schedule``."""

    def __init__(self, connection: psycopg.Connection) -> None:
        self._connection = connection

    def sent(self) -> list[dict[str, object]]:
        """The notices delivered since the last call (a notice is delivered when its transaction commits), without their ids."""
        payloads = [json.loads(notice.payload) for notice in self._connection.notifies(timeout=0.5, stop_after=None)]
        return [{key: value for key, value in payload.items() if key != "id"} for payload in payloads]


@pytest.fixture
def schedule_notices(takt: str) -> Iterator[ScheduleNotices]:
    """Listen on the channel takt listens on, to see what it is told."""
    with _test_db("test_testdb") as connection:
        connection.execute("LISTEN rekuest_schedule")
        yield ScheduleNotices(connection)


async def eventually[T](read: Callable[[], Awaitable[T | None]], seconds: float = 10.0) -> T:
    """What takt does on a notice happens after the mutation answered: ask until it is there."""
    deadline = time.monotonic() + seconds
    while True:
        found = await read()
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Nothing after {seconds:.0f} s")
        await asyncio.sleep(0.05)


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
    settings.DATABASES["default"]["PORT"] = int(os.environ.get("REKUEST_TEST_DB_PORT", settings.DATABASES["default"]["PORT"]))
    settings.AGENT_REDIS_PORT = int(os.environ.get("REKUEST_TEST_REDIS_PORT", settings.AGENT_REDIS_PORT))
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


async def waiting_run(schedule: int | str, *, other_than: int | None = None) -> int:
    """The run takt planned for the schedule, once it has (it hears of a change when that commits).

    ``other_than``: the run that was waiting before a change that replaces it.
    """
    from facade import models

    async def planned() -> int | None:
        return await models.Task.objects.filter(schedule_id=schedule, is_done=False).exclude(pk=other_than).values_list("pk", flat=True).afirst()

    return await eventually(planned)


async def settled_run(run: int | str) -> "models.Task":
    """The run once it is over: takt cancels a waiting run after the change that drops it commits."""
    from facade import models

    async def over() -> "models.Task | None":
        return await models.Task.objects.filter(pk=run, is_done=True).afirst()

    return await eventually(over)
