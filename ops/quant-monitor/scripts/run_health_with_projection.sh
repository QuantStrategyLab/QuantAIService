#!/usr/bin/env bash
# Self-hosted adapter only. The original monitor command and exit code survive.
set -u
[[ $# -eq 2 ]] || exit 2
original_entry=$1
projection_config=$2
bash "$original_entry"
health_status=$?
if [[ "$health_status" -eq 0 || "$health_status" -eq 2 ]]; then
  python3 "$(dirname "$0")/publish_saved_monitor_observation.py" --config "$projection_config" || \
    echo '{"status":"unavailable","reason":"observation_publish_failed"}' >&2
fi
exit "$health_status"
