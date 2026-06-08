#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUNS=${RUNS:-8}
SECONDS_PER_RUN=${SECONDS_PER_RUN:-8}
EVENTS=${EVENTS:-2}
KEEP_LOGS=${KEEP_LOGS:-0}

run=1
while [ "$run" -le "$RUNS" ]; do
	log=$(mktemp "${TMPDIR:-/tmp}/obs-media-source-sync-sweep.XXXXXX")
	status=0
	"$SCRIPT_DIR/run-obs-media-source-sync-test.sh" --seconds "$SECONDS_PER_RUN" --events "$EVENTS" "$@" >"$log" 2>&1 || status=$?

	printf 'RUN %02d status=%s\n' "$run" "$status"
	grep -E '^(SYNC|SUMMARY) ' "$log" | sed 's/^/  /' || true
	if [ "$KEEP_LOGS" = 1 ]; then
		printf '  log=%s\n' "$log"
	else
		rm -f "$log"
	fi

	run=$((run + 1))
done
