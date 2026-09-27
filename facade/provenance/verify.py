"""Verify a provenance token rekuest minted itself — the proof a service was called in a task.

A service that reports "this object was created in task 57" could say anything; a service that
hands back the provenance token rekuest gave task 57 cannot. So a signal carries the raw token
the service was called with, and rekuest checks it against its OWN key — issuer, signature,
expiry — before it believes the task. ``jti`` is deliberately not single-use here: one task
legitimately creates many objects, and each announces itself with the same token.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from joserfc import jwt
from joserfc.jwk import KeySet

from facade.provenance import keys

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OwnProvenance:
    """The verified claims a signal needs: which task, in which tree, for which human."""

    task: str
    root: str
    parent: str | None
    root_caused_by: str | None


def verify_own_token(raw: str | None) -> OwnProvenance | None:
    """The claims of a token this rekuest minted, or None when it is absent or not genuine."""
    if not raw:
        return None
    try:
        token = jwt.decode(raw, KeySet([keys.get_public_key()]), algorithms=keys.ALGORITHMS)
        jwt.JWTClaimsRegistry(
            iss={"essential": True, "value": keys.issuer()},
            exp={"essential": True},
            tsk={"essential": True},
        ).validate(token.claims)
    except Exception as error:  # forged, expired, another issuer's — all the same to a signal
        logger.warning("Ignoring a provenance token that does not verify: %s", error)
        return None
    claims = token.claims
    return OwnProvenance(
        task=str(claims["tsk"]),
        root=str(claims.get("rtk") or claims["tsk"]),
        parent=str(claims["ptk"]) if claims.get("ptk") else None,
        root_caused_by=claims.get("rcb"),
    )
