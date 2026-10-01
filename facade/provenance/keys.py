"""Ed25519 signing key + JWKS document for the provenance issuer.

The private key is loaded from ``settings.PROVENANCE`` (sourced from the
``provenance`` block of config.yaml, mirroring how ``lok`` keys are provided). A
static key is **required**: if none is configured the facade refuses to start
(an ephemeral keypair would sign tokens that fail to verify across process
restarts or replicas, so it is never generated).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict

from cryptography.hazmat.primitives import serialization
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from joserfc.jwk import OKPKey

logger = logging.getLogger(__name__)

# RFC 9864 fully-specified algorithm identifier for Ed25519 JWS. This replaces
# the now-deprecated generic "EdDSA" identifier (RFC 8037), which joserfc flags
# with a SecurityWarning. It must be passed explicitly wherever we sign/verify.
ALGORITHM = "Ed25519"
_lock = threading.Lock()
_signing_key: OKPKey | None = None
_public_key: OKPKey | None = None


def _kid() -> str:
    return settings.PROVENANCE["KID"]


def _load() -> None:
    """Populate the module-level signing/public keys (idempotent, thread-safe)."""
    global _signing_key, _public_key
    if _signing_key is not None:
        return

    with _lock:
        if _signing_key is not None:
            return

        conf = settings.PROVENANCE
        private_pem = conf.get("PRIVATE_KEY")
        public_pem = conf.get("PUBLIC_KEY")

        if not private_pem:
            raise ImproperlyConfigured(
                "No provenance private key configured (settings.PROVENANCE['PRIVATE_KEY']). Set the `provenance.private_key` field in config.yaml (or the `PROVENANCE__PRIVATE_KEY` environment variable) — a static Ed25519 key is required so tokens verify across process restarts and replicas."
            )

        kid = _kid()
        signing = OKPKey.import_key(private_pem, {"kid": kid})
        if public_pem:
            public = OKPKey.import_key(public_pem, {"kid": kid})
        else:
            # Derive the public JWK from the private key.
            public = OKPKey.import_key(
                signing.public_key.public_bytes(
                    serialization.Encoding.PEM,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                ),
                {"kid": kid},
            )

        _signing_key = signing
        _public_key = public


def get_public_key() -> OKPKey:
    """The Ed25519 public key used to verify provenance tokens."""
    _load()
    assert _public_key is not None
    return _public_key


def get_public_jwk() -> Dict[str, Any]:
    """The public key as a JWK dict, with ``kid``/``use``/``alg`` set."""
    jwk = get_public_key().as_dict(kid=_kid())
    jwk.setdefault("use", "sig")
    jwk.setdefault("alg", ALGORITHM)
    return jwk


def get_jwks_document() -> Dict[str, Any]:
    """The JWKS document published at the JWKS endpoint for offline verification."""
    return {"keys": [get_public_jwk()]}
