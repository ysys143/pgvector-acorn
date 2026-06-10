#!/bin/bash
# run_fastpath.sh — self-contained in-container runner for bench_fastpath.py
# Invoked as: docker run --rm -v "$PWD":/workspace -v pg_acorn_fastpath:/var/lib/postgresql/data \
#                -w /workspace pg_acorn_test bash bench/run_fastpath.sh <stage>
# PGDATA lives on a named volume so the (non-deterministic) index build
# happens once and is reused by every stage.
set -euo pipefail

STAGE=${1:?usage: run_fastpath.sh <stage>}
PG_CONFIG=${PG_CONFIG:-/usr/lib/postgresql/17/bin/pg_config}
export PGDATA=${PGDATA:-/var/lib/postgresql/data}

make -s -C /workspace PG_CONFIG="$PG_CONFIG" install

if [ ! -s "$PGDATA/PG_VERSION" ]; then
    chown postgres:postgres "$PGDATA"
    gosu postgres initdb --locale=C --encoding=UTF8 >/dev/null
fi

gosu postgres pg_ctl start -D "$PGDATA" -w -l /tmp/pg.log \
    -o "-c listen_addresses='localhost' -c shared_preload_libraries='pg_acorn' -c shared_buffers=512MB" \
    || { cat /tmp/pg.log; exit 1; }
trap 'gosu postgres pg_ctl stop -D "$PGDATA" -m fast >/dev/null' EXIT

python3 -u /workspace/bench/bench_fastpath.py --stage "$STAGE"
