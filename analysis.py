#!/usr/bin/env python3
"""Experiment runner: aggregate trials (mean, std, 95% CI) -> results/*.csv,
figures/*.png. Subcommands, one per experiment (PHASE2_PROMPT.md Step 5):

    python analysis.py switches-multifail --ks 4 8 16 24 32 --p 1 --trials 30
    python analysis.py critical-failure-pct --ks 4 8 16 24 32 --trials 10
    python analysis.py table2 --trials 10
    python analysis.py throughput-vs-failures --ks 8 16 --trials 15
    python analysis.py link-vs-switch --k 16 --trials 15
"""
import argparse
import copy
import csv
import os
import sys

import networkx as nx

from fattree.config import DEFAULT_ALLOWED_K, DEFAULTS
from fattree.failures import apply_failures
from fattree.metrics import simulate_multifail_vs_k
from fattree.routing import get_router
from fattree.sim.flow import simulate_flows
from fattree.sim.graph import connectivity_and_path_metrics, path_diversity
from fattree.topology import build_fat_tree, build_traditional_tree
from fattree.traffic import generate_traffic
from visualize import (
    plot_critical_failure_pct,
    plot_link_vs_switch_failures,
    plot_switches_multifail,
    plot_table2,
    plot_throughput_vs_failures,
)


def _write_csv(path, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {path}")


def _mean_ci95(values):
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return mean, 0.0
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return mean, 1.96 * (var ** 0.5) / (n ** 0.5)


# --- switches-multifail (Phase 1, kept) --------------------------------------------------------------------

def cmd_switches_multifail(args):
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

    _write_csv(args.csv, rows)
    print(f"wrote {plot_switches_multifail(rows, args.p, args.plot)}")


# --- critical-failure-pct-vs-k --------------------------------------------------------------------

def _mean_fraction_connected(g, pct, trials, seed):
    failures_cfg = {"link_pct": pct, "switch_pct": 0,
                     "link_layers": ["edge-agg", "agg-core"], "switch_layers": [], "mode": "bernoulli"}
    total = 0.0
    for t in range(trials):
        g2, _ = apply_failures(g, failures_cfg, seed=seed + t)
        total += connectivity_and_path_metrics(g, g2)["fraction_connected"]
    return total / trials


def _find_critical_pct(g, threshold, trials, seed, lo=0.0, hi=99.0, tol=1.0):
    """Smallest link failure % (bernoulli) where mean fraction_connected over
    `trials` seeded runs drops below `threshold`. None if not reached by `hi`."""
    if _mean_fraction_connected(g, hi, trials, seed) >= threshold:
        return None
    if _mean_fraction_connected(g, lo, trials, seed) < threshold:
        return lo
    while hi - lo > tol:
        mid = (lo + hi) / 2
        if _mean_fraction_connected(g, mid, trials, seed) < threshold:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def cmd_critical_failure_pct(args):
    rows = []
    for k in args.ks:
        g = build_fat_tree(k)
        pct_999 = _find_critical_pct(g, 0.999, args.trials, args.seed)
        pct_99 = _find_critical_pct(g, 0.99, args.trials, args.seed)
        rows.append({"k": k, "critical_pct_99_9": pct_999, "critical_pct_99": pct_99})
        print(f"k={k:>3}  critical%(99.9%% conn)={pct_999}  critical%(99%% conn)={pct_99}")

    _write_csv(args.csv, rows)
    print(f"wrote {plot_critical_failure_pct(rows, args.plot)}")


# --- table2 (paper reproduction) --------------------------------------------------------------------

PAPER_PATTERNS = [
    ("random", {"pattern": "random"}),
    ("stride1", {"pattern": "stride", "stride": 1}),
    ("stride2", {"pattern": "stride", "stride": 2}),
    ("stride4", {"pattern": "stride", "stride": 4}),
    ("stride8", {"pattern": "stride", "stride": 8}),
    ("staggered(1.0,0.0)", {"pattern": "staggered", "subnet_p": 1.0, "pod_p": 0.0}),
    ("staggered(0.5,0.3)", {"pattern": "staggered", "subnet_p": 0.5, "pod_p": 0.3}),
    ("staggered(0.2,0.3)", {"pattern": "staggered", "subnet_p": 0.2, "pod_p": 0.3}),
    ("inter-pod-incoming", {"pattern": "inter-pod-incoming"}),
    ("same-id-outgoing", {"pattern": "same-id-outgoing"}),
]

PAPER_TABLE2 = {
    "tree": [53.4, 100, 78.1, 27.9, 28.0, 100, 83.6, 64.9, 28.0, 27.8],
    "two-level": [75.0, 100, 100, 100, 100, 100, 82.0, 75.6, 50.6, 38.5],
    "classifier": [76.3, 100, 100, 100, 100, 100, 86.2, 80.2, 75.1, 75.4],
    "scheduler": [93.5, 100, 99.5, 100, 99.9, 100, 93.4, 88.5, 99.9, 87.4],
}


def _scheme_topology_and_router(scheme, k, cfg):
    if scheme == "tree":
        g = build_traditional_tree(k)
        router = get_router("single-path", g, cfg)
    else:
        g = build_fat_tree(k)
        router = get_router(scheme, g, cfg)
    return g, router


def cmd_table2(args):
    k = args.k
    rows = []
    for scheme in ("tree", "two-level", "classifier", "scheduler"):
        for name, pattern_cfg in PAPER_PATTERNS:
            pcts = []
            for t in range(args.trials):
                cfg = copy.deepcopy(DEFAULTS)
                cfg["topology"]["k"] = k
                cfg["traffic"].update(pattern_cfg)
                cfg["routing"] = scheme if scheme != "tree" else "single-path"
                g, router = _scheme_topology_and_router(scheme, k, cfg)
                flows = generate_traffic(g, cfg, seed=args.seed + t)
                if scheme == "classifier":
                    paths = router.route_all(flows)
                    result = _flow_result_from_paths(g, flows, paths, args.link_capacity_gbps, cfg["traffic"]["load"])
                else:
                    result = simulate_flows(router, flows, args.link_capacity_gbps, cfg["traffic"]["load"])
                pcts.append(result["throughput_pct_of_ideal"])
            mean, ci95 = _mean_ci95(pcts)
            rows.append({"scheme": scheme, "pattern": name, "mean_pct": mean, "ci95": ci95})
            print(f"{scheme:<11} {name:<20} {mean:6.1f}% (+/- {ci95:.1f})")

    _write_csv(args.csv, rows)
    print(f"wrote {plot_table2(rows, PAPER_PATTERNS, PAPER_TABLE2, args.plot)}")


def _flow_result_from_paths(g, flows, paths, link_capacity_gbps, load):
    """Like fattree.sim.flow.simulate_flows, but for a router (e.g. classifier)
    whose natural interface is route_all(flows) -> [path, ...] rather than one
    route(flow) call per flow."""
    from fattree.sim.flow import max_min_fair_rates

    flow_links, dropped = {}, []
    for idx, path in enumerate(paths):
        if path is None:
            dropped.append(idx)
            continue
        flow_links[idx] = list(zip(path, path[1:]))  # directed: see fattree/sim/flow.py note

    demands = {idx: load * link_capacity_gbps for idx in flow_links}
    rates = max_min_fair_rates(flow_links, demands, link_capacity_gbps)
    ideal_total = load * link_capacity_gbps * len(flows)
    achieved_total = sum(rates.values())
    return {
        "rates": rates, "dropped": dropped,
        "achieved_total_gbps": achieved_total, "ideal_total_gbps": ideal_total,
        "throughput_pct_of_ideal": (achieved_total / ideal_total * 100) if ideal_total else 0.0,
    }


# --- throughput-vs-failures --------------------------------------------------------------------

def cmd_throughput_vs_failures(args):
    schemes = ["two-level", "two-level-failover", "ecmp", "classifier", "scheduler"]
    rows = []
    for k in args.ks:
        for scheme in schemes:
            for pct in args.pcts:
                vals = []
                for t in range(args.trials):
                    cfg = copy.deepcopy(DEFAULTS)
                    cfg["topology"]["k"] = k
                    real_scheme = "two-level" if scheme == "two-level-failover" else scheme
                    cfg["routing"] = real_scheme
                    if scheme == "two-level-failover":
                        cfg["two_level"]["failover"] = True
                    cfg["failures"]["link_pct"] = pct
                    cfg["failures"]["switch_pct"] = 0
                    cfg["failures"]["mode"] = "bernoulli"

                    g = build_fat_tree(k)
                    g_after, failed = apply_failures(g, cfg["failures"], seed=args.seed + t)
                    # ECMP is stateless and routes directly over whatever graph it
                    # was built with (no on_failure() bookkeeping) -- it must see
                    # g_after to react to failures at all. two-level/classifier/
                    # scheduler track failures via on_failure() instead and only
                    # need the full node set for structural lookups, which g_after
                    # still has here since switch_pct=0 (link-only failures).
                    router = get_router(real_scheme, g_after, cfg)
                    router.on_failure(failed["failed_links"], failed["failed_switches"])
                    flows = generate_traffic(g, cfg, seed=args.seed + t + 1000)

                    if scheme == "classifier":
                        paths = router.route_all(flows)
                        result = _flow_result_from_paths(g, flows, paths, args.link_capacity_gbps,
                                                          cfg["traffic"]["load"])
                    else:
                        result = simulate_flows(router, flows, args.link_capacity_gbps, cfg["traffic"]["load"])
                    vals.append(result["throughput_pct_of_ideal"])
                mean, ci95 = _mean_ci95(vals)
                rows.append({"k": k, "scheme": scheme, "link_pct": pct, "mean_pct": mean, "ci95": ci95})
                print(f"k={k:>2} {scheme:<18} link_pct={pct:>4}%  throughput={mean:6.1f}% (+/- {ci95:.1f})")

    _write_csv(args.csv, rows)
    print(f"wrote {plot_throughput_vs_failures(rows, args.ks, schemes, args.plot)}")


# --- link-vs-switch-failures --------------------------------------------------------------------

def cmd_link_vs_switch(args):
    k = args.k
    g = build_fat_tree(k)
    rows = []
    for failure_type in ("link", "switch"):
        for pct in args.pcts:
            fraction_vals, diversity_vals = [], []
            for t in range(args.trials):
                failures_cfg = {
                    "link_pct": pct if failure_type == "link" else 0,
                    "switch_pct": pct if failure_type == "switch" else 0,
                    "link_layers": ["edge-agg", "agg-core"],
                    "switch_layers": ["edge", "agg"],
                    "mode": "bernoulli",
                }
                g2, _ = apply_failures(g, failures_cfg, seed=args.seed + t)
                m = connectivity_and_path_metrics(g, g2)
                fraction_vals.append(m["fraction_connected"])
                diversity_vals.append(path_diversity(g2, k, 0, 1))
            f_mean, f_ci = _mean_ci95(fraction_vals)
            d_mean, d_ci = _mean_ci95(diversity_vals)
            rows.append({
                "failure_type": failure_type, "pct": pct,
                "fraction_connected_mean": f_mean, "fraction_connected_ci95": f_ci,
                "path_diversity_mean": d_mean, "path_diversity_ci95": d_ci,
            })
            print(f"{failure_type:<6} pct={pct:>4}%  connected={f_mean:.4f} (+/-{f_ci:.4f})  "
                  f"diversity={d_mean:.3f} (+/-{d_ci:.3f})")

    _write_csv(args.csv, rows)
    print(f"wrote {plot_link_vs_switch_failures(rows, args.pcts, args.plot)}")


# --- CLI --------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p1 = sub.add_parser("switches-multifail", help="switches with >=2 failed links vs k (Phase 1)")
    p1.add_argument("--ks", type=int, nargs="+", default=[4, 8, 16, 24, 32])
    p1.add_argument("--p", type=float, default=1.0)
    p1.add_argument("--trials", type=int, default=30)
    p1.add_argument("--seed", type=int, default=1)
    p1.add_argument("--csv", default="results/switches_multifail_vs_k.csv")
    p1.add_argument("--plot", default="figures/switches_multifail_vs_k.png")
    p1.set_defaults(func=cmd_switches_multifail)

    p2 = sub.add_parser("critical-failure-pct", help="smallest link failure %% dropping connectivity below 99.9%%/99%%")
    p2.add_argument("--ks", type=int, nargs="+", default=[4, 8, 16, 24, 32])
    p2.add_argument("--trials", type=int, default=10)
    p2.add_argument("--seed", type=int, default=1)
    p2.add_argument("--csv", default="results/critical_failure_pct_vs_k.csv")
    p2.add_argument("--plot", default="figures/critical_failure_pct_vs_k.png")
    p2.set_defaults(func=cmd_critical_failure_pct)

    p3 = sub.add_parser("table2", help="reproduce the paper's Table 2 (tree/two-level/classifier/scheduler x 10 patterns)")
    p3.add_argument("--k", type=int, default=4)
    p3.add_argument("--trials", type=int, default=10)
    p3.add_argument("--seed", type=int, default=1)
    p3.add_argument("--link-capacity-gbps", type=float, default=0.096)
    p3.add_argument("--csv", default="results/table2_reproduction.csv")
    p3.add_argument("--plot", default="figures/table2_reproduction.png")
    p3.set_defaults(func=cmd_table2)

    p4 = sub.add_parser("throughput-vs-failures", help="throughput vs link failure %%, one curve per routing scheme")
    p4.add_argument("--ks", type=int, nargs="+", default=[8, 16])
    p4.add_argument("--pcts", type=float, nargs="+", default=[0, 5, 10, 15, 20, 25, 30])
    p4.add_argument("--trials", type=int, default=15)
    p4.add_argument("--seed", type=int, default=1)
    p4.add_argument("--link-capacity-gbps", type=float, default=1.0)
    p4.add_argument("--csv", default="results/throughput_vs_failures.csv")
    p4.add_argument("--plot", default="figures/throughput_vs_failures.png")
    p4.set_defaults(func=cmd_throughput_vs_failures)

    p5 = sub.add_parser("link-vs-switch", help="link failures vs switch failures at equal %%: connectivity + path diversity")
    p5.add_argument("--k", type=int, default=16)
    p5.add_argument("--pcts", type=float, nargs="+", default=[0, 1, 2, 5, 10, 15, 20])
    p5.add_argument("--trials", type=int, default=15)
    p5.add_argument("--seed", type=int, default=1)
    p5.add_argument("--csv", default="results/link_vs_switch_failures.csv")
    p5.add_argument("--plot", default="figures/link_vs_switch_failures.png")
    p5.set_defaults(func=cmd_link_vs_switch)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
