# The edge image: the admin console built from source + Caddy. Build context: repository root.
FROM node:20-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS admin
# the console imports the example ontologies (`../../../../../examples/...?raw`), so the build keeps the repository's relative layout
WORKDIR /repo/apps/admin
COPY apps/admin/package.json apps/admin/package-lock.json ./
RUN npm ci
COPY apps/admin ./
COPY examples /repo/examples
RUN npm run build

FROM caddy:2-alpine@sha256:d8542f48d34a9cf4e4c11a478865229840e87e4c96ea3f439101f31a5d35f75f
COPY deploy/compose/Caddyfile /etc/caddy/Caddyfile
COPY --from=admin /repo/apps/admin/dist /srv
# non-root: the binary carries cap_net_bind_service (set by the base image), so uid 10001 can bind 80/443 once the container is granted that one capability
RUN adduser -S -u 10001 -h /home/edge edge && mkdir -p /data/caddy /config/caddy && chown -R 10001 /data /config
USER 10001
# Caddy listens on 80/443 (redirect + TLS) and on 127.0.0.1:8081 inside the container for the health check only
HEALTHCHECK --interval=15s --timeout=3s CMD wget -q -O /dev/null http://127.0.0.1:8081/ready || exit 1
