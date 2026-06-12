#!/bin/bash
# run_colocation.sh — self-contained in-container runner for validate_colocation.py
# Invoked as: docker run --rm -v "$PWD":/workspace -v pg_acorn_coloc:/var/lib/postgresql/data \
#                -w /workspace pg_acorn_test bash bench/run_colocation.sh
# PGDATA lives on its OWN named volume (pg_acorn_coloc) so the index builds
# happen once and are reused; do NOT point this at the reserved
# pg_acorn_bench compose stack.
set -euo pipefail

PG_CONFIG=${PG_CONFIG:-/usr/lib/postgresql/17/bin/pg_config}
export PGDATA=${PGDATA:-/var/lib/postgresql/data}

make -s -C /workspace PG_CONFIG="$PG_CONFIG" install

if [ ! -s "$PGDATA/PG_VERSION" ]; then
    chown postgres:postgres "$PGDATA"
    gosu postgres initdb --locale=C --encoding=UTF8 >/dev/null
fi

gosu postgres pg_ctl start -D "$PGDATA" -w -l /tmp/pg.log \
    -o "-c listen_addresses='localhost' -c shared_preload_libraries='pg_acorn' -c shared_buffers=2GB -c jit=off -c max_parallel_workers_per_gather=0" \
    || { cat /tmp/pg.log; exit 1; }
trap 'gosu postgres pg_ctl stop -D "$PGDATA" -m fast >/dev/null' EXIT

python3 -u /workspace/bench/validate_colocation.py "$@"
