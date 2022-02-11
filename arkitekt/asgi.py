"""
ASGI config for elements project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/3.1/howto/deployment/asgi/
"""

import os
import django

from facade.consumers.watchman import WatchmanConsumer


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "arkitekt.settings")
django.setup(set_prefix=False)

from django.core.asgi import get_asgi_application
from facade.consumers.agent import AgentConsumer
from facade.consumers.postman import PostmanConsumer
from facade.workers.gateway import GatewayConsumer
from channels.auth import AuthMiddlewareStack
from channels.routing import ChannelNameRouter, ProtocolTypeRouter, URLRouter
from django.conf.urls import url
from django.core.asgi import get_asgi_application
from balder.consumers import MyGraphqlWsConsumer
from lok.middlewares.scope.bouncer import BouncerChannelMiddleware
from lok.middlewares.scope.jwt import JWTChannelMiddleware
from django.views.static import serve


# The channel routing defines what connections get handled by what consumers,
# selecting on either the connection type (ProtocolTypeRouter) or properties
# of the connection's scope (like URLRouter, which looks at scope["path"])
# For more, see http://channels.readthedocs.io/en/latest/topics/routing.html
def MiddleWareStack(inner):
    return AuthMiddlewareStack(JWTChannelMiddleware(BouncerChannelMiddleware(inner)))


application = ProtocolTypeRouter(
    {
        # Channels will do this for you automatically. It's included here as an example.
        "http": get_asgi_application(),
        # Route all WebSocket requests to our custom chat handler.
        # We actually don't need the URLRouter here, but we've put it in for
        # illustration. Also note the inclusion of the AuthMiddlewareStack to
        # add users and sessions - see http://channels.readthedocs.io/en/latest/topics/authentication.html
        "websocket": MiddleWareStack(
            URLRouter(
                [
                    url("graphql/", MyGraphqlWsConsumer.as_asgi()),
                    url("graphql", MyGraphqlWsConsumer.as_asgi()),
                    url(r"agent\/$", AgentConsumer.as_asgi()),
                    url(r"postman\/$", PostmanConsumer.as_asgi()),
                    url(r"watchman\/$", WatchmanConsumer.as_asgi()),
                ]
            )
        ),
        "channel": ChannelNameRouter({"gateway": GatewayConsumer.as_asgi()}),
    }
)
