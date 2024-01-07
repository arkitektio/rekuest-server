"""
ASGI config for elements project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/3.1/howto/deployment/asgi/
"""

import os
import django
from django.urls import re_path


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "arkitekt.settings")
django.setup(set_prefix=False)

from django.core.asgi import get_asgi_application
from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from django.conf.urls import url
from balder.consumers import MyGraphqlWsConsumer
from lok.middlewares.scope.bouncer import BouncerChannelMiddleware
from lok.middlewares.scope.jwt import JWTChannelMiddleware

from hare.consumers.postman.hare.postman import HarePostmanConsumer
from hare.consumers.agent.hare.agent import HareAgentConsumer

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
                    re_path("graphql/", MyGraphqlWsConsumer.as_asgi()),
                    re_path("graphql", MyGraphqlWsConsumer.as_asgi()),
                    re_path(r"watchi\/$", HarePostmanConsumer.as_asgi()),
                    re_path(r"watchi$", HarePostmanConsumer.as_asgi()),
                    re_path(r"agi\/$", HareAgentConsumer.as_asgi()),
                    re_path(r"agi$", HareAgentConsumer.as_asgi()),
                ]
            )
        ),
    }
)
