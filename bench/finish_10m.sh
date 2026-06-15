#!/bin/bash
# Recover the 10M run: the parallel build needs a maintenance_work_mem-sized DSM in
# /dev/shm (default ~63 GB < 64 GB -> "No space left on device"), so the original
# 10M build failed silently and the latency phase measured seqscans. Enlarge /dev/shm,
# rebuild the two indexes (with a guard), then re-measure. Data + Qdrant are reused.
set -uo pipefail
N=10000000; MWM=64GB; DIR=$HOME/scale_data; BENCH=$HOME/pg_acorn/bench
UV=$HOME/.local/bin/uv
export PGPASSWORD=postgres
P(){ psql -h 127.0.0.1 -U postgres -d bench "$@"; }
RUN(){ $UV run --with numpy --with "psycopg[binary]" --with httpx python3 "$@"; }

echo "###### 10M REBUILD START $(date -u) ######"
echo "### enlarge /dev/shm (DSM for parallel build)"; sudo mount -o remount,size=96G /dev/shm; df -h /dev/shm | tail -1
echo "### stop qdrant (free RAM for build)"; sudo docker stop qdrant

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

CNT=$(P -tAc "SELECT count(*) FROM pg_class WHERE relname IN ('tv_pgv_hnsw','tv_acorn_g2p64')")
[ "$CNT" = "2" ] || { echo "### BUILD GUARD FAILED ($CNT/2 indexes) - aborting"; sudo docker start qdrant; exit 1; }

echo "### restart qdrant"; sudo docker start qdrant; sleep 20
echo "### [6] qdrant settle (already indexed on disk)"
for j in $(seq 1 240); do
  idx=$(curl -s localhost:6333/collections/cohere 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin)['result'];print(d.get('indexed_vectors_count') or 0)" 2>/dev/null || echo 0)
  [ "$idx" -ge "$((N*98/100))" ] && { echo "  qdrant SETTLED indexed=$idx"; break; }
  [ $((j % 8)) = 0 ] && echo "  qdrant indexed=$idx/$N"; sleep 15
done

echo "### [7] LATENCY (recall + median/p95/p99)"
RUN $BENCH/scalebench.py --mode latency --n $N --dir $DIR
echo "### [8] THROUGHPUT (concurrency sweep)"
P -c "DROP INDEX IF EXISTS tv_pgv_hnsw"
RUN $BENCH/scalebench.py --mode throughput --n $N --config acorn_g2p64 --sel 1 --ef 100 --dir $DIR
RUN $BENCH/scalebench.py --mode throughput --n $N --config acorn_g2p64 --sel 10 --ef 400 --dir $DIR
P -c "DROP INDEX IF EXISTS tv_acorn_g2p64"
RUN $BENCH/scalebench.py --mode throughput --n $N --config pgv_prefilter --sel 1 --dir $DIR
RUN $BENCH/scalebench.py --mode throughput --n $N --config pgv_prefilter --sel 10 --dir $DIR
RUN $BENCH/scalebench.py --mode throughput --n $N --config qdrant --sel 1 --ef 100 --dir $DIR
RUN $BENCH/scalebench.py --mode throughput --n $N --config qdrant --sel 10 --ef 400 --dir $DIR
echo "###### 10M REDONE $(date -u) ######"
