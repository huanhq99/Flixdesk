#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_EXAMPLE="$ROOT_DIR/server/.env.example"
ENV_FILE="$ROOT_DIR/server/.env"

EXTERNAL_IP=""
INTERNAL_IP=""
PUBLIC_HOST=""
SERVER_PORT="9099"
ADMIN_USER=""
ADMIN_PASS=""
FORCE=0

usage() {
  cat <<'EOF'
Usage: ./scripts/init-server-env.sh --external-ip <IP> --internal-ip <IP> [options]

Options:
  --external-ip <IP>     Server public IP (required)
  --internal-ip <IP>     Server private/bind IP for Coturn (required)
  --domain <HOST>        Optional public domain advertised to clients (COTURN_PUBLIC_HOST)
  --port <PORT>          Signaling server port (default: 9099)
  --admin-user <USER>    Optional web admin username
  --admin-pass <PASS>    Optional web admin password
  --force                Overwrite existing server/.env
  -h, --help             Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --external-ip) EXTERNAL_IP="$2"; shift 2 ;;
    --internal-ip) INTERNAL_IP="$2"; shift 2 ;;
    --domain) PUBLIC_HOST="$2"; shift 2 ;;
    --port) SERVER_PORT="$2"; shift 2 ;;
    --admin-user) ADMIN_USER="$2"; shift 2 ;;
    --admin-pass) ADMIN_PASS="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ -z "$EXTERNAL_IP" || -z "$INTERNAL_IP" ]]; then
  echo "Error: --external-ip and --internal-ip are required." >&2
  usage
  exit 1
fi

if [[ -f "$ENV_FILE" && "$FORCE" -ne 1 ]]; then
  echo "Error: $ENV_FILE already exists. Pass --force to overwrite." >&2
  exit 1
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "Error: openssl is required to generate random secrets." >&2
  exit 1
fi

AUTH_SECRET="$(openssl rand -hex 32)"
NONCE_SECRET="$(openssl rand -hex 32)"

cp "$ENV_EXAMPLE" "$ENV_FILE"

python3 - "$ENV_FILE" "$EXTERNAL_IP" "$INTERNAL_IP" "$PUBLIC_HOST" "$SERVER_PORT" "$AUTH_SECRET" "$NONCE_SECRET" "$ADMIN_USER" "$ADMIN_PASS" <<'PY'
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
updates = {
    "EXTERNAL_IP": sys.argv[2],
    "INTERNAL_IP": sys.argv[3],
    "COTURN_PUBLIC_HOST": sys.argv[4],
    "CROSSDESK_SERVER_PORT": sys.argv[5],
    "COTURN_AUTH_SECRET": sys.argv[6],
    "COTURN_STATELESS_NONCE_SECRET": sys.argv[7],
    "ADMIN_USERNAME": sys.argv[8],
    "ADMIN_PASSWORD": sys.argv[9],
    "CROSSDESK_IMAGE": "ghcr.io/huanhq99/flixdesk-server:latest",
    "COTURN_REALM": "flixdesk",
}

lines = env_path.read_text(encoding="utf-8").splitlines()
out = []
for line in lines:
    replaced = False
    for k, v in updates.items():
        if line.startswith(f"{k}="):
            out.append(f"{k}={v}")
            replaced = True
            break
    if not replaced:
        out.append(line)

env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY

echo "Generated $ENV_FILE successfully."
echo "Next steps:"
echo "  1. Review $ENV_FILE"
echo "  2. Run: cd server && docker compose pull && docker compose up -d --no-build"
