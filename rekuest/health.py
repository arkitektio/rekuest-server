"""This server's health covers takt: the agents' half of rekuest.

Without takt the server still answers queries, but nothing can be assigned, controlled or
registered and no agent can connect — for anyone asking ``ht``, rekuest is down. So ``ht`` asks
takt's own ``ht`` (its database and redis), and asks it where this server asks takt everything:
at its internal listener. Both of takt's listeners answer ``ht``, so that alone would pass for a
server pointed at the address agents reach, which has none of the internal API; a route only
the internal listener has is asked too.
"""

import dataclasses

import httpx
from django.conf import settings
from health_check.base import HealthCheck
from health_check.exceptions import ServiceUnavailable

from facade import takt

TIMEOUT_SECONDS = 2.0


@dataclasses.dataclass
class Takt(HealthCheck):
    """takt answers its health check."""

    async def run(self) -> None:
        """Ask takt's ``ht`` and for its internal API; unavailable unless both are there."""
        base = settings.TAKT_URL.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=httpx.AsyncHTTPTransport(uds=takt.socket())) as client:
                health = await client.get(f"{base}/ht")
                # A GET of a POST route: 405 where the route is, 404 where the internal API is not.
                internal = await client.get(f"{base}/internal/assign")
        except httpx.HTTPError as error:
            raise ServiceUnavailable("takt is unreachable") from error
        if health.status_code != 200:
            raise ServiceUnavailable(f"takt is unhealthy ({health.status_code})")
        if internal.status_code == 404:
            raise ServiceUnavailable("takt's internal API is not at rekuest.takt_url / rekuest.takt_socket: that is the address agents reach")
