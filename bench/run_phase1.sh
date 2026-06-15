#!/bin/bash
# Phase 1 of the scaling study: 100K + 1M at 30 maintenance workers, then STOP.
# We measure 1M build time empirically and project 10M before committing to it
# (and decide whether to raise machine specs). Launched under nohup on the VM.
set -uo pipefail
DIR=$HOME/scale_data; B=$HOME/pg_acorn/bench
[ -f "$DIR/queries.npy" ] || { echo "### NO DATA"; exit 1; }
ls -lh "$DIR"/*.npy

bash "$B/run_scale.sh" 100000  4GB  0
bash "$B/run_scale.sh" 1000000 16GB 0

echo "###### PHASE1_DONE (100K+1M) $(date -u) ######"
