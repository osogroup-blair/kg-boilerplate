#!/usr/bin/env bash
# One-time setup for the sandbox host: secret files, and a random router key shared by the platform and the mock router. Never overwrites an existing secret.
set -euo pipefail
cd "$(dirname "$0")/../.."
SECRETS=${KG_SECRETS_DIR:-deploy/compose/secrets}
mkdir -p "$SECRETS"
KEYFILE="$(mktemp)"
trap 'rm -f "$KEYFILE"' EXIT
if [ -f .env.sandbox ] && grep -q '^SANDBOX_ROUTER_KEY=.\+' .env.sandbox; then
  grep '^SANDBOX_ROUTER_KEY=' .env.sandbox | cut -d= -f2- | tr -d '\n' > "$KEYFILE"
else
  python3 -c 'import secrets; print("sbx_" + secrets.token_urlsafe(36), end="")' > "$KEYFILE"
fi
python3 scripts/prod_secrets.py --out "$SECRETS" --router-key-file "$KEYFILE"
for f in turnstile_secret sandbox_ip_salt; do
  [ -f "$SECRETS/$f" ] || { python3 -c 'import secrets; print(secrets.token_urlsafe(32), end="")' > "$SECRETS/$f"; chmod 0444 "$SECRETS/$f"; }
done
echo "  note: turnstile_secret holds a random value. To enable the Cloudflare Turnstile check, replace it with the secret key from the Cloudflare dashboard."
[ -f .env.sandbox ] || cp deploy/sandbox/sandbox.env.example .env.sandbox
sed -i "s|^SANDBOX_ROUTER_KEY=.*|SANDBOX_ROUTER_KEY=$(cat "$KEYFILE")|" .env.sandbox
chmod 0600 .env.sandbox
echo "ready: edit .env.sandbox (host, TLS contact, site origin), then see deploy/sandbox/README.md"
