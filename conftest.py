# content of conftest.py
import pytest
import smtplib
from lok.models import LokApp
from lok.middlewares.scope.jwt import JWTChannelMiddleware
from django.contrib.auth import get_user_model
from asgiref.sync import sync_to_async
from lok.token import JwtToken
from channels.db import database_sync_to_async
from facade import models


@pytest.fixture()
@pytest.mark.django_db
def mock_jwt_authentication(monkeypatch):
    async def mocked_call(self, scope, receive, send):
        user, created = await database_sync_to_async(
            get_user_model().objects.update_or_create
        )(username="test", email="test_email")
        app, created = await database_sync_to_async(LokApp.objects.update_or_create)(
            client_id="test", name="test"
        )
        scope["auth"] = JwtToken(
            {
                "iss": 1,
                "scope": "everything",
                "roles": ["team:sibarita"],
                "type": "implicit",
            },
            user,
            app,
            "sdfsdfsdf",
        )
        scope["user"] = user
        return await self.app(scope, receive, send)

    monkeypatch.setattr(
        JWTChannelMiddleware,
        "__call__",
        mocked_call,
    )


@pytest.fixture()
@pytest.mark.django_db
async def reservable_node():
    def crate_node():

        postmanapp, _ = LokApp.objects.update_or_create(
            client_id="postman", name="test"
        )

        agentapp, _ = LokApp.objects.update_or_create(client_id="agent", name="test")

        user, _ = get_user_model().objects.update_or_create(
            username="test", email="test_email"
        )

        x = models.Node.objects.create(name="test", interface="test", package="test")

        r = models.Registry.objects.create(app=agentapp, user=user)

        a = models.Agent.objects.create(registry=r)

        t = models.Template.objects.create(name="test", node=x, registry=r)
        return x

    return await sync_to_async(crate_node)()
