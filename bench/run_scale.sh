#!/bin/bash
# Per-scale orchestration: load -> build (timed) -> qdrant settle -> latency -> throughput.
# Usage: run_scale.sh N MWM STOP_QDRANT_FOR_BUILD(0/1)
set -uo pipefail
N=${1:?N}; MWM=${2:-16GB}; STOPQ=${3:-0}
DIR=$HOME/scale_data; UV=$HOME/.local/bin/uv; BENCH=$HOME/pg_acorn/bench
export PGPASSWORD=postgres
P(){ psql -h 127.0.0.1 -U postgres -d bench "$@"; }
RUN(){ $UV run --with numpy --with "psycopg[binary]" --with httpx python3 "$@"; }

echo "###### N=$N MWM=$MWM START $(date -u) ######"

# Parallel index build puts a maintenance_work_mem-sized DSM in /dev/shm; the default
# (~50% RAM) is < 64 GB, so a large-MWM build fails with "No space left on device".
echo "### ensure /dev/shm fits the parallel-build DSM"
sudo mount -o remount,size=96G /dev/shm 2>/dev/null || true

echo "### [1] load PG (COPY) + truth"
RUN $BENCH/scale_load.py --n $N --dir $DIR --skip-qdrant

echo "### [2] upload Qdrant"
RUN $BENCH/scale_load.py --n $N --dir $DIR --skip-pg

[ "$STOPQ" = "1" ] && { echo "### stop qdrant (free RAM for build)"; sudo docker stop qdrant; }

echo "### [3] build pgvector hnsw (timed)"
P -c "SET maintenance_work_mem='$MWM'" -c "SET max_parallel_maintenance_workers=30" -c "\timing on" \
  -c "DROP INDEX IF EXISTS tv_pgv_hnsw" \
  -c "CREATE INDEX tv_pgv_hnsw ON tv_items USING hnsw (embedding vector_cosine_ops) WITH (m=16,ef_construction=64)"

echo "### [4] build acorn g2p64 (timed)"
P -c "SET maintenance_work_mem='$MWM'" -c "SET max_parallel_maintenance_workers=30" -c "SET pg_acorn.build_seed=42" -c "\timing on" \
  -c "DROP INDEX IF EXISTS tv_acorn_g2p64" \
  -c "CREATE INDEX tv_acorn_g2p64 ON tv_items USING acorn_hnsw (embedding vector_cosine_ops, bucket int4_acorn_ops) WITH (m=16,ef_construction=64,acorn_gamma=2,acorn_payload_edges=true,acorn_payload_m=64,acorn_inline_vectors=false)"

echo "### [5] index sizes"
P -c "SELECT relname,pg_size_pretty(pg_relation_size(oid)) sz FROM pg_class WHERE relname IN ('tv_pgv_hnsw','tv_acorn_g2p64')"

# Guard: a failed build (e.g. DSM/disk) must not silently fall through to seqscan
# measurement. Both indexes must exist before we measure.
CNT=$(P -tAc "SELECT count(*) FROM pg_class WHERE relname IN ('tv_pgv_hnsw','tv_acorn_g2p64')")
[ "$CNT" = "2" ] || { echo "### BUILD GUARD FAILED ($CNT/2 indexes) - aborting N=$N"; [ "$STOPQ" = "1" ] && sudo docker start qdrant; exit 1; }

[ "$STOPQ" = "1" ] && { echo "### restart qdrant (reload from disk)"; sudo docker start qdrant; sleep 20; }

echo "### [6] qdrant settle (indexed>=N)"
for j in $(seq 1 240); do
  idx=$(curl -s localhost:6333/collections/cohere 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin)['result'];print(d.get('indexed_vectors_count') or 0)" 2>/dev/null || echo 0)
  [ "$idx" -ge "$((N*98/100))" ] && { echo "  qdrant SETTLED indexed=$idx"; break; }
  [ $((j % 8)) = 0 ] && echo "  qdrant indexed=$idx/$N"
  sleep 15
done

echo "### [7] LATENCY (recall + median/p95/p99) all engines"
RUN $BENCH/scalebench.py --mode latency --n $N --dir $DIR

echo "### [8] THROUGHPUT (concurrency sweep)"
P -c "DROP INDEX IF EXISTS tv_pgv_hnsw"     # isolate acorn_g2p64
RUN $BENCH/scalebench.py --mode throughput --n $N --config acorn_g2p64 --sel 1 --ef 100 --dir $DIR
RUN $BENCH/scalebench.py --mode throughput --n $N --config acorn_g2p64 --sel 10 --ef 400 --dir $DIR
P -c "DROP INDEX IF EXISTS tv_acorn_g2p64"  # isolate prefilter (bitmap only)
RUN $BENCH/scalebench.py --mode throughput --n $N --config pgv_prefilter --sel 1 --dir $DIR
RUN $BENCH/scalebench.py --mode throughput --n $N --config pgv_prefilter --sel 10 --dir $DIR
RUN $BENCH/scalebench.py --mode throughput --n $N --config qdrant --sel 1 --ef 100 --dir $DIR
RUN $BENCH/scalebench.py --mode throughput --n $N --config qdrant --sel 10 --ef 400 --dir $DIR

echo "###### N=$N DONE $(date -u) ######"
