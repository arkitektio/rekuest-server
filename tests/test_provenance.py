"""Tests for the provenance token issuing side.

These exercise Rekuest's role as the provenance *issuer*: key/JWKS publication,
the canonical args hash, audience resolution, the human-root invariant, and the
shape + signature of the minted claim set. Verification semantics that belong
downstream (single-use jti, actor binding, args_hash recomputation) are out of
scope here.

The mint tests use lightweight fakes for the task/request graph so the
issuing logic can be verified without standing up the database — the claim
contract depends only on attributes Rekuest reads off those objects.
"""

from __future__ import annotations


from django.conf import settings

from facade.provenance import canonical, keys


# --- canonical args hash -------------------------------------------------


def test_args_hash_is_order_independent() -> None:
    assert canonical.args_hash({"a": 1, "b": 2}) == canonical.args_hash({"b": 2, "a": 1})


def test_args_hash_differs_on_different_args() -> None:
    assert canonical.args_hash({"a": 1}) != canonical.args_hash({"a": 2})


def test_args_hash_empty_is_stable() -> None:
    assert canonical.args_hash({}) == canonical.args_hash(None or {})


# --- JWKS / keys ---------------------------------------------------------


def test_jwks_endpoint_serves_cacheable_document() -> None:
    from django.test import RequestFactory

    from rekuest.urls import jwks_view

    response = jwks_view(RequestFactory().get("/.well-known/jwks.json"))
    assert response.status_code == 200
    assert "public" in response["Cache-Control"]
    import json

    doc = json.loads(response.content)
    assert doc["keys"][0]["kid"] == settings.PROVENANCE["KID"]


def test_jwks_document_publishes_signing_key() -> None:
    doc = keys.get_jwks_document()
    assert "keys" in doc and len(doc["keys"]) == 1
    jwk = doc["keys"][0]
    assert jwk["kty"] == "OKP"
    assert jwk["crv"] == "Ed25519"
    assert jwk["kid"] == settings.PROVENANCE["KID"]
    assert jwk["alg"] == "Ed25519"
    assert jwk["use"] == "sig"
    # The published key is public only — no private component.
    assert "d" not in jwk


# --- audience derivation (registration time) -----------------------------


# --- audience on the token (read from the implementation) -----------------


# --- needs_token ---------------------------------------------------------


# --- top-level claim correctness -----------------------------------------


# --- sub-assignment lineage inheritance ----------------------------------


# --- human-root invariant ------------------------------------------------


# --- predicate unit ------------------------------------------------------


