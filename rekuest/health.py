"""This server's health covers takt: the agents' half of rekuest.

Without takt the server still answers queries, but nothing can be assigned, controlled or
registered and no agent can connect — for anyone asking ``ht``, rekuest is down. So ``ht`` asks
takt's own ``ht`` (its database and redis) on its internal listener.
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
        """Ask takt's ``ht``; unavailable unless it answers 200."""
        url = f"{settings.TAKT_URL.rstrip('/')}/ht"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=httpx.AsyncHTTPTransport(uds=takt.socket())) as client:
                response = await client.get(url)
        except httpx.HTTPError as error:
            raise ServiceUnavailable("takt is unreachable") from error
        if response.status_code != 200:
            raise ServiceUnavailable(f"takt is unhealthy ({response.status_code})")
