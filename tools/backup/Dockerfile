# The backup tool image: pg_dump / pg_restore / psql of the SAME major version as the database (the digest is the database image's), plus `age` for encryption.
# Pinned by digest. Built on demand by `python -m tools.backup build-image`; nothing in it holds a key or a credential.
FROM pgvector/pgvector:pg17@sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d
RUN apt-get update \
 && apt-get install -y --no-install-recommends age ca-certificates \
 && rm -rf /var/lib/apt/lists/*
USER postgres
ENTRYPOINT []
