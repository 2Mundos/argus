#!/bin/sh
# Without LITESTREAM_REPLICA_URL, run Argus directly (Litestream is optional).
# With it: restore if the database is missing and a replica exists, then run Argus
# as a child of `litestream replicate` so the WAL is shipped continuously.
set -eu

if [ -z "${LITESTREAM_REPLICA_URL:-}" ]; then
  exec "$@"
fi

litestream restore -config /etc/litestream.yml \
  -if-db-not-exists -if-replica-exists "${ARGUS_DATA_DIR}/argus.db"
exec litestream replicate -config /etc/litestream.yml -exec "$*"
