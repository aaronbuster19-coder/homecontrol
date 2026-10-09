#!/bin/sh
# Run every browser test and the Node unit tests inside the Playwright image, exactly like CI's e2e job.
#   e2e/docker.sh                 # all tests
#   e2e/docker.sh -k heating -x   # extra pytest arguments
# Own network namespace (no --network host): the servers bind 127.0.0.1 inside the container on free ports.
# The checkout is mounted read-only; failure screenshots/traces (and the tests' own screenshots) go to $E2E_OUT
# (default e2e/artifacts), owned by you. E2E_DOCKER_ARGS adds docker run options (e.g. a proxy CA for pip).
set -eu
IMAGE=mcr.microsoft.com/playwright/python:v1.56.0-noble  # keep in step with playwright== in requirements-e2e.txt
cd "$(dirname "$0")/.."
OUT=${E2E_OUT:-$PWD/e2e/artifacts}
CACHE=${E2E_PIP_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/homecontrol-e2e-pip}
mkdir -p "$OUT" "$CACHE"
exec docker run --rm --init --shm-size=1g ${E2E_CONTAINER:+--name "$E2E_CONTAINER"} \
  -v "$PWD":/src:ro -v "$OUT":/out -v "$CACHE":/root/.cache/pip \
  -e E2E_TRACE="${E2E_TRACE:-1}" -e E2E_ARTIFACTS=/out -e SHOTS=/out/shots -e E2E_REQUIRE_NODE=1 \
  -e OWNER="$(id -u):$(id -g)" ${E2E_DOCKER_ARGS:-} \
  "$IMAGE" sh /src/e2e/docker-inside.sh "$@"
