"""Validate the rekuest service's config.yaml against its bespoke schema.

Standalone — needs no database; run with ``uv run pytest tests/test_config.py``.
"""

from rekuest.configuration import Settings, unread


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
    """The ``embeddings`` block defaults to potion-base-8M at 256 dims and is env-overridable."""
    s = Settings()
    assert s.embeddings.enabled is True
    assert s.embeddings.model == "minishlab/potion-base-8M"
    assert s.embeddings.dimensions == 256
    assert s.embeddings.model_path is None

    monkeypatch.setenv("EMBEDDINGS__DISTANCE_THRESHOLD", "0.42")
    monkeypatch.setenv("EMBEDDINGS__MODEL_PATH", "/opt/models/embeddings")
    s = Settings()
    assert s.embeddings.distance_threshold == 0.42
    assert s.embeddings.model_path == "/opt/models/embeddings"


def test_the_services_own_config_is_read_as_written():
    """Nothing in the repo's config.yaml goes unread."""
    assert not unread()


def test_a_key_of_another_release_is_reported_not_swallowed():
    """A hub's config written for rekuest 5: the list it read is unknown to this release."""
    found = unread(
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
    found = unread({"postgres": {"sslmode": "require"}, "redis": {"db": 2}, "lok": {"url": "http://lok"}, "datalayer": {"media": {"bucket": "m", "acl": "private"}}})
    assert not found
