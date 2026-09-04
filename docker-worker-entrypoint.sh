#!/bin/bash
set -e

# The shared /app/media bind volume is written by the web service (root).
# Grant the unprivileged render user ownership so render outputs can be saved.
chown -R appuser:appuser /app/media /app/temp_uploads 2>/dev/null || true

exec gosu appuser "$@"
