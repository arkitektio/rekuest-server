"""What this image answers a hub's installer, asked the way the installer asks it.

Standalone — needs no database; run with ``uv run pytest tests/test_contract.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from hub_contract import cli

PEM = "-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEILK+rl9gVEjfKGiye+mLLjfEUGIdoP0WPC8lMZS3NYK2\n-----END PRIVATE KEY-----\n"

FACTS: dict[str, object] = {
    "me": {"name": "rekuest", "path": "rekuest", "url": "http://rekuest:80/rekuest", "identifier": "live.arkitekt.rekuest", "secret_key": "s3cret", "admin": {"username": "admin", "password": "pw"}},
    "hub": {"origins": ["https://lab.example"], "auth": {"audience": "*", "issuers": [{"kind": "jwks_uri", "iss": "lok", "jwks_uri": "http://lok/.well-known/jwks.json"}], "static_tokens": {}}},
    "database": {"host": "db", "name": "rekuest", "username": "hub", "password": "pw"},
    "redis": {"host": "redis"},
    "storage": {"host": "rustfs", "port": 9000, "access_key": "a", "secret_key": "s", "buckets": {"media": "rekuestmedia"}},
    "instance": {"private_key": PEM, "trust": {"jwks_uri": "http://lok/.well-known/hub-keys/1"}},
    "peers": {
        "takt": {"url": "http://rekuest-takt:8080/rekuest", "settings": {"socket": "/run/takt/internal.sock"}},
        "mikro": {"identifier": "live.arkitekt.mikro", "url": "http://mikro:80/mikro", "offers": {"rekuest_service": "http://mikro:80/mikro/_rekuest/service", "rekuest_hook": "http://mikro:80/mikro/_rekuest/hook"}},
        "kraph": {"identifier": "live.arkitekt.kraph", "url": "http://kraph:80/kraph"},
        "notes": {"url": "http://notes:80/notes", "offers": {"rekuest_hook": "http://notes:80/notes/_rekuest/hook"}},
    },
}


@pytest.fixture(autouse=True)
def this_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """The contract is this service's."""
    monkeypatch.setenv("HUB_CONTRACT", "rekuest.contract")


def rendered(tmp_path: Path, capsys: pytest.CaptureFixture[str], facts: dict[str, object], overrides: dict[str, object] | None = None) -> tuple[int, str, str]:
    """``render`` asked on the command line: its exit code, what it printed, what it said."""
    (tmp_path / "facts.yaml").write_text(yaml.safe_dump(facts), encoding="utf-8")
    if overrides is not None:
        (tmp_path / "overrides.yaml").write_text(yaml.safe_dump(overrides), encoding="utf-8")
    code = cli.main(["render", "--facts", str(tmp_path / "facts.yaml"), "--overrides", str(tmp_path / "overrides.yaml")])
    said = capsys.readouterr()
    return code, said.out, said.err


def test_it_says_what_it_needs_before_it_has_any_config(capsys: pytest.CaptureFixture[str]) -> None:
    """``describe`` needs no config, and names what a hub has to provide."""
    assert cli.main(["describe"]) == 0

    said = json.loads(capsys.readouterr().out)
    assert said["name"] == "rekuest"
    assert said["needs"]["instance_key"] is True and said["needs"]["storage"] == ["media"] and said["needs"]["peers"] == ["takt"]


def test_its_config_is_written_from_what_the_hub_says(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The hub's facts in, this release's config out — one it starts on."""
    code, out, err = rendered(tmp_path, capsys, FACTS)
    assert code == 0, err

    config = yaml.safe_load(out)
    block = config["rekuest"]
    # Wired to whoever offers each endpoint — a service, a hook agent, or both — by what the
    # peer offers and not by a list of names anybody keeps.
    assert block["services"] == [{"name": "mikro", "url": "http://mikro:80/mikro/_rekuest/service", "identifier": "live.arkitekt.mikro"}]
    assert block["hook_agents"] == [
        {"name": "mikro", "hook_url": "http://mikro:80/mikro/_rekuest/hook", "identifier": "live.arkitekt.mikro"},
        {"name": "notes", "hook_url": "http://notes:80/notes/_rekuest/hook"},
    ]
    assert block["takt_url"] == "http://rekuest-takt:8080/rekuest" and block["takt_socket"] == "/run/takt/internal.sock"
    assert block["server_url"] == "http://rekuest:80/rekuest"
    assert config["datalayer"]["media"] == {"bucket": "rekuestmedia"}
    assert config["django"]["force_script_name"] == "rekuest" and config["django"]["csrf_trusted_origins"] == ["https://lab.example"]
    assert config["instance"]["trust"] == {"jwks_uri": "http://lok/.well-known/hub-keys/1"}


def test_what_the_operator_set_is_in_it_and_what_this_release_does_not_read_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An override is kept; one naming a key of another release stops the render by name."""
    code, out, _ = rendered(tmp_path, capsys, FACTS, {"rekuest": {"task_retention": 2592000}})
    assert code == 0 and yaml.safe_load(out)["rekuest"]["task_retention"] == 2592000

    code, out, err = rendered(tmp_path, capsys, FACTS, {"rekuest": {"service_agents": []}})
    assert code == cli.REFUSED and out == ""
    assert "rekuest.service_agents" in err


def test_a_hub_it_cannot_run_in_is_refused_in_words(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No takt, no rekuest: said, not rendered into a config that starts and does nothing."""
    without_takt = {**FACTS, "peers": {name: peer for name, peer in FACTS["peers"].items() if name != "takt"}}  # type: ignore[union-attr]
    code, out, err = rendered(tmp_path, capsys, without_takt)
    assert code == cli.REFUSED and out == ""
    assert "takt" in err
