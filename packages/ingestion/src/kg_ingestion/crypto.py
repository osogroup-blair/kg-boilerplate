"""Payload encryption at rest and keyed content hashing (ADR-024).

PayloadCipher: AES-256-GCM, a random 96-bit nonce per encryption, a keyring (kid -> 32 bytes), and associated data
`workspace_id|interaction_id|form`, so a ciphertext copied to another row or tenant fails to decrypt.
ContentHasher: HMAC-SHA256 under a per-workspace key derived with HKDF from CONTENT_HMAC_SECRET. A plain hash of low-entropy text could be
dictionary-attacked; the keyed hash still supports per-workspace dedupe statistics. Its master is NOT the payload key, so rotating payload keys
never changes request hashes (which would break idempotent replay)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from kg_common.errors import PlatformError
from kg_common.secrets import SecretRing
from kg_common.settings import Settings

KID = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
KEY_BYTES = 32
NONCE_BYTES = 12


class PayloadUnreadable(PlatformError):
    """A stored payload cannot be decrypted (unknown key id, or the ciphertext does not belong to this row). Never a 4xx: it is a platform fault."""

    code, http_status, default_message = "payload_unreadable", 500, "A stored payload could not be decrypted"


class KeyringError(ValueError):
    pass


@dataclass(frozen=True)
class Sealed:
    kid: str
    nonce: bytes
    ciphertext: bytes


def parse_keyring(spec: str) -> dict[str, bytes]:
    ring: dict[str, bytes] = {}
    for part in (p.strip() for p in spec.split(",")):
        if not part:
            continue
        kid, sep, b64 = part.partition(":")
        if not sep or not KID.match(kid):
            raise KeyringError("PAYLOAD_KEYS must look like kid:base64key,kid:base64key")
        try:
            key = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise KeyringError(f"payload key '{kid}' is not valid base64") from exc
        if len(key) != KEY_BYTES:
            raise KeyringError(f"payload key '{kid}' must be {KEY_BYTES} bytes")
        if kid in ring:
            raise KeyringError(f"duplicate payload key id '{kid}'")
        ring[kid] = key
    if not ring:
        raise KeyringError("PAYLOAD_KEYS is empty")
    return ring


def aad(workspace_id: uuid.UUID, interaction_id: uuid.UUID, form: str) -> bytes:
    return f"{workspace_id}|{interaction_id}|{form}".encode()


class PayloadCipher:
    def __init__(self, keyring: Mapping[str, bytes], active_kid: str):
        if active_kid not in keyring:
            raise KeyringError(f"PAYLOAD_ACTIVE_KID '{active_kid}' is not in PAYLOAD_KEYS")
        self._aead = {kid: AESGCM(key) for kid, key in keyring.items()}
        self.active_kid = active_kid

    @classmethod
    def from_settings(cls, settings: Settings) -> PayloadCipher:
        return cls(parse_keyring(settings.payload_keys.get_secret_value()), settings.payload_active_kid)

    @property
    def kids(self) -> list[str]:
        return sorted(self._aead)

    def encrypt(self, plaintext: bytes, *, workspace_id: uuid.UUID, interaction_id: uuid.UUID, form: str, kid: str | None = None) -> Sealed:
        use = kid or self.active_kid
        nonce = os.urandom(NONCE_BYTES)
        return Sealed(use, nonce, self._aead[use].encrypt(nonce, plaintext, aad(workspace_id, interaction_id, form)))

    def decrypt(self, sealed: Sealed, *, workspace_id: uuid.UUID, interaction_id: uuid.UUID, form: str) -> bytes:
        aead = self._aead.get(sealed.kid)
        if aead is None:
            raise PayloadUnreadable(details={"reason": "unknown_key_id"})
        try:
            return aead.decrypt(bytes(sealed.nonce), bytes(sealed.ciphertext), aad(workspace_id, interaction_id, form))
        except InvalidTag as exc:
            raise PayloadUnreadable(details={"reason": "integrity_check_failed"}) from exc


class ContentHasher:
    """Keyed hashes over a secret RING (P9 rotation). New hashes use the ACTIVE version; every check of a hash that was stored earlier tries every version in the ring
    (`*_all` returns the active one first), so adding a version, making it active and later removing the old one never makes a stored hash unrecognisable (idempotent replay,
    memory dedupe, the suppression of a deleted subject's identifiers)."""

    def __init__(self, secret: str | bytes | SecretRing):
        if isinstance(secret, SecretRing):
            self._ring = secret
        else:
            self._ring = SecretRing({"v1": secret.encode() if isinstance(secret, str) else secret}, "v1")

    @classmethod
    def from_settings(cls, settings: Settings) -> ContentHasher:
        return cls(settings.content_hmac_ring())

    @property
    def versions(self) -> list[str]:
        return self._ring.versions

    @staticmethod
    def _key(workspace_id: uuid.UUID, master: bytes) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=f"kg-content-hmac|{workspace_id}".encode()).derive(master)

    def digest(self, workspace_id: uuid.UUID, data: bytes) -> str:
        return hmac.new(self._key(workspace_id, self._ring.current), data, hashlib.sha256).hexdigest()

    def digest_all(self, workspace_id: uuid.UUID, data: bytes) -> list[str]:
        """The digest under every version still in the ring, the active one first."""
        return [hmac.new(self._key(workspace_id, secret), data, hashlib.sha256).hexdigest() for _, secret in self._ring.ordered()]

    def digest_json(self, workspace_id: uuid.UUID, value: Any) -> str:
        return self.digest(workspace_id, canonical_json(value))

    def digest_json_all(self, workspace_id: uuid.UUID, value: Any) -> list[str]:
        return self.digest_all(workspace_id, canonical_json(value))

    def ref(self, workspace_id: uuid.UUID, ref_type: str, system: str, value: str) -> str:
        """Keyed hash of an external identifier, so an unresolved reference can be matched later without storing the value in the clear."""
        return self.digest(workspace_id, f"ref|{ref_type}|{system}|{value}".encode())

    def ref_all(self, workspace_id: uuid.UUID, ref_type: str, system: str, value: str) -> list[str]:
        return self.digest_all(workspace_id, f"ref|{ref_type}|{system}|{value}".encode())


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()
