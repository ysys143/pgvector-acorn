#!/bin/bash
# Crash repro: hammer sel=1% ef=1600 cache-mode queries (the bench protocol
# shape) and watch for backend death.  Run inside the debug container.
# Usage: bash /workspace/bench/cc_crash_repro.sh <iterations>
ITER=${1:-200}
QV=$(python3 -c "import json;print(json.load(open('/tmp/qv.json')))")

for i in $(seq 1 "$ITER"); do
  out=$(gosu postgres psql -qAt 2>&1 <<EOF
SET enable_seqscan = off;
SET enable_bitmapscan = off;
SET pg_acorn.member_first = on;
SET pg_acorn.scan_inline_vectors = on;
SET pg_acorn.scan_code_cache = on;
SET pg_acorn.ef_search = 1600;
BEGIN;
DROP INDEX tv_acorn_idx;
SELECT count(*) FROM (SELECT id FROM tv_items WHERE bucket < 1
  ORDER BY embedding <=> '$QV'::vector LIMIT 10) s;
SELECT count(*) FROM (SELECT id FROM tv_items WHERE bucket < 10
  ORDER BY embedding <=> '$QV'::vector LIMIT 10) s;
ROLLBACK;
EOF
)
  rc=$?
  if [ $rc -ne 0 ]; then
    echo "ITERATION $i FAILED (rc=$rc):"
    echo "$out"
    echo "=== cores ==="
    find / -name "core*" -newer /tmp/qv.json 2>/dev/null | grep -v proc
    exit 1
  fi
  [ $((i % 20)) -eq 0 ] && echo "iter $i ok"
done
echo "no crash in $ITER iterations"
