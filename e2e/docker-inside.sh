#!/bin/sh
# Runs inside the Playwright image (see e2e/docker.sh): Node for tests/*.test.js, the pinned requirements (straight
# into the throwaway container's Python), then the whole suite on a writable copy of the read-only checkout.
set -eu
trap 'chown -R "$OWNER" /out /root/.cache/pip 2>/dev/null || true' EXIT
if ! command -v node >/dev/null; then
  apt-get update -qq >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends nodejs >/dev/null 2>&1
fi
cp -r /src /work && cd /work && rm -rf e2e/artifacts e2e/screenshots
python -m pip install -q --disable-pip-version-check --root-user-action=ignore --break-system-packages \
  -r requirements-e2e.txt
python -m pytest e2e -p no:cacheprovider -ra "$@"
