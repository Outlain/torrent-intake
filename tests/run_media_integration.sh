#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "$0")/.."
test_dir=$(mktemp -d -t torrent-intake-media.XXXXXXXX)
clamd_id=""
cleanup() {
    if [[ -n "$clamd_id" ]]; then
        docker logs "$clamd_id"
        docker rm -f "$clamd_id" >/dev/null
    fi
    # Only this invocation's mktemp directory; no deployment volumes are used.
    rm -r -- "$test_dir"
}
trap cleanup EXIT

common=(--read-only --network none --user "$(id -u):$(id -g)"
    --cap-drop ALL --security-opt no-new-privileges --pids-limit 128 --memory 512m
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m
    --mount "type=bind,src=$test_dir,dst=/test")
# This test runs as the host UID, so use its private fixture volume for settings
# rather than the image-owned default /app/data directory.
application=(--rm "${common[@]}" --env PYTHONPATH=/app --env TI_DATA_DIR=/test/app-data --env TI_TEST_BENCHMARK_WINDOWS
    --mount "type=bind,src=$PWD/tests,dst=/tests,readonly" --entrypoint python
    "${TI_TEST_APP_IMAGE:-torrent-intake:test}" /tests/integration_media.py)

docker run "${application[@]}" prepare
clamd_id=$(docker run -d "${common[@]}" --entrypoint clamd \
    "${TI_TEST_CLAMD_IMAGE:-torrent-intake-clamd:test}" --config-file=/test/clamd.conf)
docker run "${application[@]}" scan
