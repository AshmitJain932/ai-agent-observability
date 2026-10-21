"""API key format, generation and hashing.

Keys are ``aobs_ik_`` followed by 32 random bytes (base64url, 43 characters). Only the SHA-256
of the full key and a short display prefix are stored. Plain SHA-256 is adequate here because the
key is 256 bits of randomness (nothing to brute-force, unlike a password); lookup is by hash
through a unique index, so no secret is ever compared in Python.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass

from ..errors import Unauthorized

KEY_PREFIX = "aobs_ik_"
DISPLAY_PREFIX_LENGTH = len(KEY_PREFIX) + 4
_KEY_RE = re.compile(r"^aobs_ik_[A-Za-z0-9_-]{43}\Z")
_BEARER_RE = re.compile(r"^[Bb][Ee][Aa][Rr][Ee][Rr] ([^\s]+)\Z")


@dataclass(frozen=True)
class NewKey:
    secret: str
    prefix: str
    key_hash: bytes

    def __repr__(self) -> str:  # never show the secret by accident
        return f"NewKey(prefix={self.prefix!r})"


def hash_key(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("ascii")).digest()


def generate_key() -> NewKey:
    secret = KEY_PREFIX + secrets.token_urlsafe(32)
    return NewKey(secret=secret, prefix=secret[:DISPLAY_PREFIX_LENGTH], key_hash=hash_key(secret))


def is_well_formed(secret: str) -> bool:
    return _KEY_RE.match(secret) is not None


def parse_bearer(header: str | None) -> bytes:
    """Return the hash of the key in an ``Authorization: Bearer`` header, or raise Unauthorized."""
    if not header:
        raise Unauthorized()
    match = _BEARER_RE.match(header.strip())
    if match is None or not is_well_formed(match.group(1)):
        raise Unauthorized()
    return hash_key(match.group(1))
