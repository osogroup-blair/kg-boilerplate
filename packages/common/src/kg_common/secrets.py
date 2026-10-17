"""Versioned secrets (phase-9 plan WP6): a RING of `version:secret` pairs with one ACTIVE version.

Rotation is adding a version, making it active, waiting until nothing still depends on the old one, then removing the old one. The ring is what lets the platform do all
three without a flag day: new work uses the active version; checks against stored values try every version still in the ring. A single legacy secret is the ring {first version: secret}.
Secrets are never logged and never raised in an error message (only their version names are)."""

from __future__ import annotations

import re
from dataclasses import dataclass

VERSION = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
MIN_BYTES = 32


class RingError(ValueError):
    pass


@dataclass(frozen=True)
class SecretRing:
    secrets: dict[str, bytes]
    active: str

    @classmethod
    def parse(
        cls,
        spec: str,
        active: str,
        *,
        legacy: str | None = None,
        legacy_version: str = "v1",
        name: str = "secret ring",
        min_bytes: int = MIN_BYTES,
    ) -> SecretRing:
        """`spec` is "v1:secret,v2:secret"; empty falls back to `legacy` (a single secret) under `legacy_version`."""
        ring: dict[str, bytes] = {}
        for part in (p.strip() for p in spec.split(",")):
            if not part:
                continue
            version, sep, secret = part.partition(":")
            if not sep or not VERSION.match(version) or not secret:
                raise RingError(f"{name} must look like version:secret,version:secret")
            if version in ring:
                raise RingError(f"{name} names version '{version}' twice")
            ring[version] = secret.encode()
        if not ring:
            if not legacy:
                raise RingError(f"{name} is empty")
            ring[legacy_version] = legacy.encode()
        chosen = active or (legacy_version if legacy_version in ring else next(iter(ring)))
        if chosen not in ring:
            raise RingError(f"the active version '{chosen}' is not in {name}")
        short = sorted(v for v, s in ring.items() if len(s) < min_bytes)
        if short:
            raise RingError(f"{name}: version(s) {short} are shorter than {min_bytes} bytes")
        return cls(ring, chosen)

    def get(self, version: str) -> bytes | None:
        return self.secrets.get(version)

    @property
    def current(self) -> bytes:
        return self.secrets[self.active]

    def ordered(self) -> list[tuple[str, bytes]]:
        """The active version first, then the others (newest-written first is not knowable, so by name)."""
        return [(self.active, self.secrets[self.active]), *sorted((v, s) for v, s in self.secrets.items() if v != self.active)]

    @property
    def versions(self) -> list[str]:
        return [v for v, _ in self.ordered()]
