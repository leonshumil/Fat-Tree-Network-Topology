#!/usr/bin/env bash
# One-command reproduction of this submission's tests and three experiments
# (PHASE5.md Step 5). Results are deterministic for the seeds used below --
# rerunning should reproduce every number in the README exactly.
set -euo pipefail

start_ts=$(date +%s)

echo "=== tests ==="
python3 -m pytest -q

echo "=== Experiment 1: Resilience ==="
python3 analysis.py e1-resilience --ks 8 16 32 \
    --pcts 0 5 10 15 20 25 30 35 40 45 50 55 60 --trials 30 \
    --layer-k 16 --switch-pcts 5 10

echo "=== Experiment 2: Speed under failures, and the hash-collision ceiling ==="
python3 analysis.py e2-throughput --k 16 --pcts 0 5 10 15 20 25 30 \
    --ks 4 8 16 24 32 --trials 20

echo "=== Experiment 3: Incast ==="
python3 analysis.py e3-incast --ms 1 2 4 8 12 --queue-sizes 32 64 128 \
    --time-buffer 64 --compare-m 8 --rounds 20 --trials 10

end_ts=$(date +%s)

echo
echo "=== done ==="
echo "CSVs written to results/e{1,2,3}_*.csv"
echo "figures written to figures/e{1,2,3}_*.png"
echo "total runtime: $((end_ts - start_ts))s"
