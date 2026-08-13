#!/usr/bin/env bash
set -euo pipefail

# Start a separately cloned RSSHub checkout as a local Node process.

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
RSSHUB_DIR=${RSSHUB_DIR:-"$PROJECT_ROOT/../RSSHub"}

if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
  echo "Missing project environment file: $PROJECT_ROOT/.env" >&2
  exit 1
fi

if [[ ! -f "$RSSHUB_DIR/package.json" ]]; then
  echo "RSSHub checkout not found: $RSSHUB_DIR" >&2
  echo "Clone RSSHub there or set RSSHUB_DIR to its checkout path." >&2
  exit 1
fi

node_is_supported() {
  "$1" -e '
  const [major, minor, patch] = process.versions.node.split(".").map(Number);
  const supported = (major === 22 && (minor > 22 || (minor === 22 && patch >= 2)))
    || (major === 24 && (minor > 15 || (minor === 15 && patch >= 0)));
  process.exit(supported ? 0 : 1);
  '
}

if ! command -v node >/dev/null 2>&1 || ! node_is_supported "$(command -v node)"; then
  RSSHUB_NVM_DIR=${NVM_DIR:-"${HOME:-}/.nvm"}
  if [[ -s "$RSSHUB_NVM_DIR/nvm.sh" ]]; then
    set +u
    # shellcheck disable=SC1090
    . "$RSSHUB_NVM_DIR/nvm.sh"
    set -u
    for RSSHUB_NODE_MAJOR in 24 22; do
      if nvm use --silent "$RSSHUB_NODE_MAJOR" >/dev/null 2>&1 && node_is_supported "$(command -v node)"; then
        break
      fi
    done
  fi
fi

if ! command -v node >/dev/null 2>&1 || ! node_is_supported "$(command -v node)"; then
  echo "Unsupported Node.js version: $(node --version). Use Node 22.22.2+ or 24.15.0+." >&2
  exit 1
fi

if command -v corepack >/dev/null 2>&1; then
  PNPM=(corepack pnpm)
elif command -v pnpm >/dev/null 2>&1; then
  PNPM=(pnpm)
else
  echo "pnpm is required; enable it with corepack or install pnpm 10." >&2
  exit 1
fi

# Load the project root .env without evaluating values as shell code.
unset RSSHUB_PORT PROXY_URI PORT
while IFS='=' read -r env_key env_value; do
  case "$env_key" in
    ''|\#*) continue ;;
    *) export "$env_key=$env_value" ;;
  esac
done < "$PROJECT_ROOT/.env"

if [[ -z "${RSSHUB_PORT:-}" ]]; then
  echo "Missing RSSHUB_PORT in $PROJECT_ROOT/.env" >&2
  exit 1
fi
if ! [[ "$RSSHUB_PORT" =~ ^[0-9]+$ ]] || (( RSSHUB_PORT < 1 || RSSHUB_PORT > 65535 )); then
  echo "Invalid RSSHUB_PORT in $PROJECT_ROOT/.env: $RSSHUB_PORT" >&2
  exit 1
fi

# An empty PROXY_URI explicitly disables the proxy for the Node process.
if [[ -z "${PROXY_URI:-}" ]]; then
  unset PROXY_URI
fi

# This project intentionally uses RSSHub's TWITTER_AUTH_TOKEN web API path.
# Do not pass OAuth or third-party API configuration to the local process.
unset TWITTER_CONSUMER_KEY TWITTER_CONSUMER_SECRET
unset TWITTER_ACCESS_TOKEN TWITTER_ACCESS_SECRET
unset TWITTER_THIRD_PARTY_API
export PORT="$RSSHUB_PORT"
export NODE_ENV=production

if [[ ! -f "$RSSHUB_DIR/dist/index.mjs" ]]; then
  echo "RSSHub is not built: $RSSHUB_DIR/dist/index.mjs" >&2
  echo "Run: (cd \"$RSSHUB_DIR\" && ${PNPM[*]} install --frozen-lockfile && ${PNPM[*]} build)" >&2
  exit 1
fi

echo "Starting local RSSHub from $RSSHUB_DIR on port $PORT"
echo "RSSHub proxy: ${PROXY_URI:-disabled}"
cd "$RSSHUB_DIR"
exec "${PNPM[@]}" start
