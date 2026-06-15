#!/bin/bash
# Drive the full scaling study autonomously: wait for the prepared fixture,
# then run 100K -> 1M -> 10M sequentially. Launched once under nohup on the VM.
set -uo pipefail
DIR=$HOME/scale_data; B=$HOME/pg_acorn/bench

echo "### [wait] for download (queries.npy) $(date -u)"
for j in $(seq 1 480); do                       # up to 4h
  [ -f "$DIR/queries.npy" ] && [ -f "$DIR/emb.npy" ] && { echo "### data ready $(date -u)"; break; }
  [ $((j % 10)) = 0 ] && echo "  still waiting ($((j/2)) min) ..."
  sleep 30
done
[ -f "$DIR/queries.npy" ] || { echo "### DATA TIMEOUT"; exit 1; }
ls -lh "$DIR"

bash "$B/run_scale.sh" 100000   4GB  0
bash "$B/run_scale.sh" 1000000  16GB 0
bash "$B/run_scale.sh" 10000000 64GB 1

echo "###### ALL_SCALES_DONE $(date -u) ######"
