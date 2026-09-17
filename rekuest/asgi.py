"""
ASGI config for rekuest project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.2/howto/deployment/asgi/
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rekuest.settings")
from django.core.asgi import get_asgi_application

# Initialize Django ASGI application early to ensure the AppRegistry
# is populated before importing code that may import ORM models.
django_asgi_app = get_asgi_application()


from facade.schema import schema  # noqa: E402
from kante.router import router  # noqa: E402
from facade.consumers.async_consumer import AgentConsumer  # noqa: E402
from kante.path import re_dynamicpath  # noqa: E402


websocket_urlpatterns = [
    re_dynamicpath(r"agi", AgentConsumer.as_asgi()),
]

_routed_application = router(
    django_asgi_app=django_asgi_app,
    schema=schema,
    additional_websocket_urlpatterns=websocket_urlpatterns,
    schema_path="schema",
)


# --- The in-process reconciler (``facade.reaper``) ------------------------------------------
# Every deadline the backend enforces (disconnect grace, pickup watchdog, cancel escalation,
# expiry, …) is a DB column swept by a loop inside THIS process — there is no management
# command, cron or sidecar to run, and any number of backends may run it side by side.
#
# Daphne implements no ASGI ``lifespan``, but it installs its asyncio-backed Twisted reactor
# before importing this module, so ``callWhenRunning`` starts the loop the moment the server's
# event loop is up — before (and without) any request. The scope wrapper below is the
# server-agnostic fallback; both are idempotent.
import sys  # noqa: E402

from facade.reaper import ensure_reaper_started  # noqa: E402

if "twisted.internet.reactor" in sys.modules:
    sys.modules["twisted.internet.reactor"].callWhenRunning(ensure_reaper_started)


async def application(scope, receive, send):
    ensure_reaper_started()
    return await _routed_application(scope, receive, send)
