"""Canonical args encoding + hash.

The provenance token carries ``args_hash`` rather than the inline args, which
keeps it flat and binds the (cleartext) args travelling alongside it to the
signature. The canonical form is a **versioned contract**: any downstream
verifier must reproduce exactly this encoding to recompute the hash, so changes
here are breaking and must bump ``CANONICALIZATION_VERSION``.

v1: ``json.dumps`` with sorted keys, no insignificant whitespace, non-ASCII left
as UTF-8, then SHA-256 of the UTF-8 bytes, hex-encoded.
"""

from __future__ import annotations

import hashlib
import json

from facade.json_types import JSON

CANONICALIZATION_VERSION = 1


def canonicalize_args(args: dict[str, JSON]) -> bytes:
    """Serialize args to the canonical byte form (v1)."""
    return json.dumps(
        args or {},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def args_hash(args: dict[str, JSON]) -> str:
    """SHA-256 hex digest of the canonicalized args."""
    return hashlib.sha256(canonicalize_args(args)).hexdigest()
