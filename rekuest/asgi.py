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
# The agent protocol (``/agi``: sockets, HookAgent intake, signals) is takt's; this serves
# GraphQL and its subscriptions only.
application = router(
    django_asgi_app=django_asgi_app,
    schema=schema,
    schema_path="schema",
)
