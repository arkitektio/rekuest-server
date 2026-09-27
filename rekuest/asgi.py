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

# Every deadline, schedule and delayed task is swept by the reaper, which runs in its own
# process (``python manage.py reaper`` — the ``rekuest-reaper`` container), never in here.
application = router(
    django_asgi_app=django_asgi_app,
    schema=schema,
    additional_websocket_urlpatterns=websocket_urlpatterns,
    schema_path="schema",
)
