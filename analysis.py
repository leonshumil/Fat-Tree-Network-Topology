#!/usr/bin/env python3
"""Sweep: switches with >= 2 failed links at a fixed link failure rate, as a
function of switch port count k. This is the assignment's "intermediate" example
metric (Sec. 5): "Number of switches with multiple failed links (at a 1% failure
rate) as a function of switch port count."

Compares Monte-Carlo simulation (mean +/- 95% CI over `--trials` seeded runs)
against the closed-form prediction `1 - (1-p)^k - k*p*(1-p)^(k-1)` (CLAUDE.md
Sec. 6), assuming independent (bernoulli) per-link failures across all three
link layers, so every switch's k ports are equally exposed.

Usage:
    python analysis.py --ks 4 8 16 24 32 --p 1 --trials 30 --seed 1
"""
import argparse
import csv
import os
import sys

from fattree.config import DEFAULT_ALLOWED_K
from fattree.metrics import simulate_multifail_vs_k
from fattree.topology import build_fat_tree
from visualize import plot_switches_multifail


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ks", type=int, nargs="+", default=[4, 8, 16, 24, 32],
                        help=f"switch port counts to sweep, must be in allowed_k {DEFAULT_ALLOWED_K}")
    parser.add_argument("--p", type=float, default=1.0, help="link failure percentage (default 1%%)")
    parser.add_argument("--trials", type=int, default=30, help="seeded trials per k")
    parser.add_argument("--seed", type=int, default=1, help="base seed; trial t uses seed + t")
    parser.add_argument("--csv", default="results/switches_multifail_vs_k.csv")
    parser.add_argument("--plot", default="figures/switches_multifail_vs_k.png")
    args = parser.parse_args()

    bad_ks = [k for k in args.ks if k not in DEFAULT_ALLOWED_K]
    if bad_ks:
        allowed = ", ".join(str(v) for v in DEFAULT_ALLOWED_K)
        print(f"error: --ks has values not in allowed_k ({allowed}): {bad_ks}", file=sys.stderr)
        sys.exit(1)

    rows = []
    for k in args.ks:
        g = build_fat_tree(k)
        row = simulate_multifail_vs_k(g, k, args.p, args.trials, args.seed)
        rows.append(row)
        print(f"k={k:>3}  mean={row['mean']:.2f}  theory={row['theory']:.2f}  "
              f"(+/- {row['ci95']:.2f}, {row['total_switches']} switches)")

    os.makedirs(os.path.dirname(args.csv) or ".", exist_ok=True)
    with open(args.csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {args.csv}")

    path = plot_switches_multifail(rows, args.p, args.plot)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
