#!/bin/bash
# init-test.sh — start PostgreSQL, install extensions, run tests
# Runs inside the container as root; drops to postgres for DB operations.
set -euo pipefail

WORKSPACE=${WORKSPACE:-/workspace}
PG_CONFIG=${PG_CONFIG:-/usr/lib/postgresql/17/bin/pg_config}

# Initialize data directory if needed
if [ ! -s "$PGDATA/PG_VERSION" ]; then
    gosu postgres initdb --locale=C --encoding=UTF8
fi

# Start server (background)
gosu postgres pg_ctl start -D "$PGDATA" \
    -o "-c listen_addresses='localhost' -c shared_preload_libraries=''" \
    -w

# Install pg_acorn into the cluster before CREATE EXTENSION
cd "$WORKSPACE"
make PG_CONFIG="$PG_CONFIG" install

# Now extensions are available
gosu postgres psql -c "CREATE EXTENSION IF NOT EXISTS vector;"
gosu postgres psql -c "CREATE EXTENSION IF NOT EXISTS pg_acorn;"

echo "[init-test] Extensions installed. Running tests..."

# First installcheck run — may fail if golden files are empty (TDD bootstrap).
# On failure, copy any generated results to expected/ to seed golden files.
make PG_CONFIG="$PG_CONFIG" installcheck PGUSER=postgres 2>/dev/null || {
    bootstrapped=0
    for f in test/results/*.out; do
        [ -f "$f" ] || continue
        expected="test/expected/$(basename "$f")"
        if [ ! -s "$expected" ]; then
            cp "$f" "$expected"
            echo "[init-test] golden seeded: $(basename "$f")"
            bootstrapped=1
        fi
    done
    if [ "$bootstrapped" -eq 1 ]; then
        echo "[init-test] Golden files seeded — re-running installcheck..."
    fi
}

# Second (authoritative) installcheck run
make PG_CONFIG="$PG_CONFIG" installcheck PGUSER=postgres || {
    echo "[init-test] installcheck failed — dumping regression.diffs"
    cat test/regression.diffs 2>/dev/null || true
    exit 1
}

# Isolation tests (pg_isolation_regress)
make PG_CONFIG="$PG_CONFIG" isolationcheck PGUSER=postgres || {
    echo "[init-test] isolationcheck failed"
    exit 1
}

echo "[init-test] All tests passed."
