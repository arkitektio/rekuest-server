"""Validate the rekuest service's config.yaml against its bespoke schema.

Standalone — needs no database; run with ``uv run pytest tests/test_config.py``.
"""

from arkitekt_service.contract.unread import unread
from arkitekt_service.server.settings import written

from rekuest.configuration import Settings


def test_config_yaml_validates():
    """The service's own config.yaml parses into the typed schema."""
    s = Settings()
    assert s.postgres.db_name
    assert s.redis.host


def test_env_override(monkeypatch):
    """Env vars override the YAML file (nested via ``__``)."""
    monkeypatch.setenv("POSTGRES__PASSWORD", "from-env-test")
    assert Settings().postgres.password == "from-env-test"


def test_embeddings_block_defaults_and_override(monkeypatch):
    """The ``embeddings`` block is on by default and env-overridable; the model is not in it."""
    s = Settings()
    assert s.embeddings.enabled is True
    assert set(type(s.embeddings).model_fields) == {"enabled", "distance_threshold"}

    monkeypatch.setenv("EMBEDDINGS__DISTANCE_THRESHOLD", "0.42")
    assert Settings().embeddings.distance_threshold == 0.42


def test_a_model_named_in_the_config_is_reported_as_unread():
    """The model is a constant of the release: a config that names one is told so."""
    assert unread(Settings, {"embeddings": {"enabled": True, "model": "some/other-model"}}).unknown == ["embeddings.model"]


def test_the_services_own_config_is_read_as_written():
    """Nothing in the repo's config.yaml goes unread."""
    assert not unread(Settings, written())


def test_a_key_of_another_release_is_reported_not_swallowed():
    """A hub's config written for rekuest 5: the list it read is unknown to this release."""
    found = unread(
        Settings,
        {
            "rekuest": {
                "service_agents": [{"service": "mikro", "hook_url": "http://mikro/_rekuest/hook"}],
                "agentd_url": "http://takt:8081/rekuest",
                "hook_agents": [{"name": "mikro", "hook_url": "http://mikro/_rekuest/hook", "secret": "s"}],
                "server_url": "http://rekuest:80/rekuest",
            },
            "instance": {"private_key": "k", "trust": {"jwks_url": "http://lok/keys"}},
        }
    )
    assert found.unknown == ["rekuest.service_agents", "rekuest.hook_agents[0].secret", "instance.trust.jwks_url"]
    assert found.renamed == [("rekuest.agentd_url", "rekuest.takt_url")]


def test_open_blocks_and_the_top_level_are_not_reported():
    """Driver options in a connection block, and blocks other services read, are nobody's mistake."""
    found = unread(Settings, {"postgres": {"sslmode": "require"}, "redis": {"db": 2}, "lok": {"url": "http://lok"}, "datalayer": {"media": {"bucket": "m", "acl": "private"}}})
    assert not found
