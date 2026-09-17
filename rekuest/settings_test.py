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

# Default tests to grace=0 → disconnects cascade inline/immediately (the legacy,
# deterministic behavior). The reclaim/grace tests opt into a window with override_settings.
REKUEST_GRACE = {"DEFAULT": 0, "PHYSICAL": 0}

# The in-process reaper sweeps every DB-held deadline in the background. Tests call the sweeps
# (``persist_backend.reconcile_*`` / ``expire_*`` / ``escalate_*``) explicitly instead, so a
# background pass can never race a test's own assertions.
REKUEST_REAPER_ENABLED = False

# Point the agent queue at the published dokker redis port (see
# tests/integration/docker-compose.yaml). Replaces the old redis-factory monkeypatch.
AGENT_REDIS_HOST = "localhost"
AGENT_REDIS_PORT = int(os.environ.get("REKUEST_TEST_REDIS_PORT", 6666))

# Probes: short TTLs so expiry behavior is testable without waiting.
TASK_RETENTION_SECONDS = 0
PROBE_TTL_SECONDS = 60
PROBE_LINGER_SECONDS = 30
PROBE_MAX_INFLIGHT_PER_CALLER = 8
