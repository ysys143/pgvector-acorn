#!/bin/bash
# run_audit.sh — self-contained in-container runner for build/graph audits.
# Invoked as: docker run --rm -v "$PWD":/workspace -w /workspace \
#                pg_acorn_test bash bench/run_audit.sh <script.py> [args...]
# PGDATA is container-local (anonymous volume): every run starts clean —
# builds are deterministic via pg_acorn.build_seed, so nothing needs reuse.
# Does NOT touch the compose stack project pg_acorn_bench.
set -euo pipefail

SCRIPT=${1:?usage: run_audit.sh <script.py> [args...]}
shift
PG_CONFIG=${PG_CONFIG:-/usr/lib/postgresql/17/bin/pg_config}
export PGDATA=${PGDATA:-/var/lib/postgresql/data}

make -s -C /workspace PG_CONFIG="$PG_CONFIG" install

if [ ! -s "$PGDATA/PG_VERSION" ]; then
    chown postgres:postgres "$PGDATA"
    gosu postgres initdb --locale=C --encoding=UTF8 >/dev/null
fi

gosu postgres pg_ctl start -D "$PGDATA" -w -l /tmp/pg.log \
    -o "-c listen_addresses='localhost' -c shared_preload_libraries='pg_acorn' \
        -c shared_buffers=1GB -c maintenance_work_mem=512MB \
        -c max_parallel_workers_per_gather=0 -c jit=off" \
    || { cat /tmp/pg.log; exit 1; }
trap 'gosu postgres pg_ctl stop -D "$PGDATA" -m fast >/dev/null' EXIT

python3 -u "/workspace/$SCRIPT" "$@"
