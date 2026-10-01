from .settings import *  # noqa
from .settings import DATABASES, AUTHENTIKATE, DATALAYER
import logging
import os


# There is no STS to assume a role against under unit tests, and a grant that cannot be scoped
# now refuses rather than quietly returning this service's permanent key. Tests that exercise a
# grant care about its *shape*, not its credentials, so let them have the unscoped one.
DATALAYER = {**DATALAYER, "allow_unscoped_fallback": True}

DATABASES["default"] = {**DATABASES["default"], "NAME": "testdb", "PORT": int(os.environ.get("REKUEST_TEST_DB_PORT", 5555)), "HOST": "localhost", "USER": "test", "PASSWORD": "test"}
# Django forces DEBUG=False under the test runner, and authentikate 3.0 refuses static
# tokens when DEBUG is False. These are deliberate test fixtures, so opt in explicitly.
AUTHENTIKATE = {
    **AUTHENTIKATE,
    "allow_static_tokens_in_production": True,
    "static_tokens": {
        "test": {"sub": "1", "client_id": "oinsoins", "app": "test-app"},
        # A second distinct identity (same default ``static_org``) for cross-agent tests —
        # agent 1 (token "test") assigns to agent 2 (token "test2").
        "test2": {"sub": "2", "client_id": "oinsoins2", "app": "test-app"},
        # A third identity in a *different* organization for cross-tenant tests. The auth
        # extension always derives the request's organization from the bearer token, so a
        # second tenant can only be expressed as a second token.
        "test-other": {"sub": "3", "client_id": "oinsoins3", "app": "test-app", "org": "other_org"},
    },
}


# For faster test execution, you can uncomment this:
# MIGRATION_MODULES = DisableMigrations()

# Disable logging during tests to reduce noise
logging.disable(logging.CRITICAL)

# Enable database access from async code in tests
DATABASE_ROUTERS = []

# Use in-memory channel layer for tests instead of Redis
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

# Point the agent queue at the published dokker redis port (see
# tests/integration/docker-compose.yaml). Replaces the old redis-factory monkeypatch.
AGENT_REDIS_HOST = "localhost"
AGENT_REDIS_PORT = int(os.environ.get("REKUEST_TEST_REDIS_PORT", 6666))

TASK_RETENTION_SECONDS = 0
PROBE_MAX_INFLIGHT_PER_CALLER = 8

# The hub trust bundle under test, inline: rekuest's own key plus one key per service the tests
# play (each a separate ``rekuest_service.Service(key=...)``, as separate processes would be).
from joserfc.jwk import OKPKey as _TestOKPKey  # noqa: E402
from rekuest_service.trust import public_jwk as _public_jwk  # noqa: E402

from .settings import INSTANCE, REKUEST_IDENTIFIER  # noqa: E402

TEST_SERVICE_KEYS = {name: _TestOKPKey.generate_key("Ed25519") for name in ("mikro", "housekeeping", "bank")}
INSTANCE = {
    **INSTANCE,
    "TRUST_JWKS": {
        "keys": [
            {**_public_jwk(_TestOKPKey.import_key(INSTANCE["PRIVATE_KEY"])), "service": REKUEST_IDENTIFIER},
            *({**_public_jwk(key), "service": f"live.arkitekt.{name}"} for name, key in TEST_SERVICE_KEYS.items()),
        ]
    },
}
