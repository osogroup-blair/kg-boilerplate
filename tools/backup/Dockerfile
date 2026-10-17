# The backup tool image: pg_dump / pg_restore / psql of the SAME major version as the database (the digest is the database image's), plus `age` for encryption.
# Pinned by digest. Built on demand by `python -m tools.backup build-image`; nothing in it holds a key or a credential.
FROM pgvector/pgvector:pg17@sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d
# `upgrade` pulls the distribution's security fixes over the pinned base (trivy gates this image in `make scan`).
RUN apt-get update \
 && apt-get -y upgrade \
 && apt-get install -y --no-install-recommends age ca-certificates \
 && rm -rf /var/lib/apt/lists/* /usr/local/bin/gosu  # gosu (a Go binary with open CVEs) is the database image's entrypoint helper; the tool runs as `postgres` directly
USER postgres
ENTRYPOINT []
