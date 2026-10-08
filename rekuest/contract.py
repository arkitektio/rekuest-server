"""What this image answers a hub's installer: ``arkitekt-service <verb>`` (see ``arkitekt_service.contract``).

The installer knows the hub; how this release spells its config is written here, with the
settings it is read by. A key renamed in ``configuration.py`` is renamed in :func:`render` in
the same commit, and no installer has to learn of it.
"""

from __future__ import annotations

from arkitekt_service.contract import JSON, Contract, Description, Facts, Job, Needs, Offers, Peer, Refused, Scope, Sidecar, Start, blocks

from rekuest.configuration import Settings

#: The peer that is this server's other half: the agents' endpoint and the internal API.
TAKT = "takt"

#: What a token may be allowed to do here: defined at the coordination server when the hub enrols.
SCOPES = [
    Scope(key="rekuest_agent", description="Act as an agent"),
    Scope(key="rekuest_call", description="Call other apps with rekuest"),
    Scope(key="read", description="Read access to rekuest resources"),
    Scope(key="write", description="Write access to rekuest resources"),
]

#: The roles a member of an organization can hold here.
ROLES = [
    Scope(key="agent", description="Can act as a workflow agent"),
    Scope(key="caller", description="Can call remote procedures"),
    Scope(key="admin", description="Full administrative access"),
]


def _service(name: str, peer: Peer) -> dict[str, JSON]:
    """One ``rekuest.services`` entry: where the service is, who it signs as, and what it hosts when the hub says."""
    return {
        "name": name,
        "url": peer.offers["rekuest_service"],
        **({"identifier": peer.identifier} if peer.identifier else {}),
        **({"hosts": peer.hosts.model_dump(mode="json")} if peer.hosts is not None else {}),
    }


def render(facts: Facts) -> dict[str, JSON]:
    """This release's config for the hub ``facts`` describes."""
    takt = facts.peers.get(TAKT)
    if takt is None:
        raise Refused("it needs takt beside it: without takt nothing can be assigned and no agent can connect")

    document: dict[str, JSON] = {
        **blocks.server(facts),
        "instance": blocks.instance(facts),
        "provenance": {"issuer": facts.me.settings.get("provenance_issuer", "rekuest")},
        "rekuest": {
            # A service and a hook agent are separate entries: one catalogues what exists
            # there, the other what can be done there. A peer may offer either or both.
            # What a service hosts goes in with it when the hub knows it (its image said so):
            # the catalog is then written from here, and the service is not asked.
            "services": [_service(name, peer) for name, peer in facts.offering("rekuest_service").items()],
            "hook_agents": [{"name": name, "hook_url": peer.offers["rekuest_hook"], **({"identifier": peer.identifier} if peer.identifier else {})} for name, peer in facts.offering("rekuest_hook").items()],
            "identifier": facts.me.identifier,
            "server_url": facts.me.url,
            "takt_url": takt.url,
            **({"takt_socket": takt.settings["socket"]} if "socket" in takt.settings else {}),
        },
    }
    if facts.storage is not None:
        document["datalayer"] = blocks.datalayer(facts)
    return document


contract = Contract(
    description=Description(
        name="rekuest",
        identifier="live.arkitekt.rekuest",
        summary="Assigns work to agents and keeps the record of it.",
        needs=Needs(scopes=SCOPES, roles=ROLES, storage=["media"], instance_key=True, peers=[TAKT]),
        offers=Offers(health="ht"),
        # Its other half: every agent connects to takt, not to this server, and the two are
        # released together under the same tag (takt even names the migrations it expects).
        sidecars=[Sidecar(name="takt", image="{repository}-takt:{tag}", summary="The agent protocol: registration, assignment, liveness.")],
        upgrade_from="5.0.0",
    ),
    settings=Settings,
    render=render,
    # How this service is started: there is no script beside it. `arkitekt-service serve`
    # (and `debug`) become these, so they get the container's signals themselves.
    serve=Start(("daphne", "-b", "0.0.0.0", "-p", "80", "--websocket_timeout", "-1", "rekuest.asgi:application")),
    debug=Start(("python", "manage.py", "runserver", "0.0.0.0:80")),
    # The catalog of what the hub's services host is written with the database, from what the
    # config says of each (`rekuest.services[].hosts`): there before anything serves, and an
    # installer runs it again by name when a service was added, without restarting rekuest.
    jobs={
        "ensureadmin": Job(("ensureadmin",), "Create the operator account the config names"),
        "catalogue": Job(("catalogue",), "Catalogue what the hub's services host, from the config"),
    },
    setup=("ensureadmin", "catalogue"),
)
