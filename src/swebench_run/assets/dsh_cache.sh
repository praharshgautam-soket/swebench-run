#!/bin/sh
# Runs in the cache pod (node image). Installs the pinned agent ONCE, packs it into a checksummed
# tarball, and serves it to task pods inside the cluster. Env: PACKAGE, VERSION, PORT.
set -eu
export HOME=/tmp npm_config_cache=/tmp/.npm
mkdir -p /srv/work && cd /srv/work
npm init -y >/dev/null 2>&1
npm install --no-audit --no-fund --loglevel=error "${PACKAGE}@${VERSION}"

# Refuse to serve anything but the exact pinned version.
INSTALLED=$(node -p "require('./node_modules/${PACKAGE}/package.json').version")
if [ "$INSTALLED" != "$VERSION" ]; then
  echo "installed ${INSTALLED}, wanted ${VERSION}" >&2
  exit 1
fi

# Task pods run on a different image, so ship a node binary with the agent.
mkdir -p bin && cp "$(command -v node)" bin/node

tar -czf "/srv/dsh-${VERSION}.tar.gz" -C /srv/work .
cd /srv && sha256sum "dsh-${VERSION}.tar.gz" > "dsh-${VERSION}.tar.gz.sha256"
touch /srv/READY
rm -rf /tmp/.npm
echo "cache ready: ${PACKAGE}@${VERSION}"
exec node /app/cache_serve.js
