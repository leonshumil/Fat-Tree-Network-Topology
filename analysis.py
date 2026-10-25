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
import itertools
import os
import random
import sys

import networkx as nx

from fattree.config import DEFAULT_ALLOWED_K, DEFAULTS
from fattree.failures import apply_failures
from fattree.metrics import simulate_multifail_vs_k
from fattree.routing import get_router
from fattree.routing.precomputed import PrecomputedPathRouter
from fattree.sim.flow import simulate_flows
from fattree.sim.graph import connectivity_and_path_metrics, path_diversity
from fattree.sim.packet import PacketSimulator, simulate_packets
from fattree.topology import build_fat_tree, build_traditional_tree, hosts_of
from fattree.traffic import Flow, generate_pairs, generate_poisson_flows, generate_traffic
from visualize import (
    plot_critical_failure_pct,
    plot_e1_connectivity,
    plot_e1_layer,
    plot_e1_path_diversity,
    plot_e2_collision_ceiling,
    plot_e2_vs_failures,
    plot_e3_collapse,
    plot_e3_queue,
    plot_e3_where_time_goes,
    plot_incast_drop_location,
    plot_incast_goodput_vs_m,
    plot_incast_queue_series,
    plot_incast_timeouts_vs_m,
    plot_link_vs_switch_failures,
    plot_mice_elephants_bars,
    plot_mice_elephants_bytes_pie,
    plot_mice_elephants_line,
    plot_mice_elephants_mouse_fct_cdf,
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


# --- e1-resilience (Phase 4, PHASE4.md Step 4; PHASE5.md Step 1) --------------------------------------------------------------------

def _e1_pod_pairs(k, seed):
    """Pod pairs to average path_diversity over: all C(k,2) pairs when
    that's small enough (k <= 8), else a fixed seeded sample of 8 -- a
    single representative pod pair was a noisy sample on its own, even
    after path_diversity's own per-pair averaging over edge switches
    (PHASE5.md Step 1)."""
    all_pairs = list(itertools.combinations(range(k), 2))
    if k <= 8:
        return all_pairs
    return random.Random(seed).sample(all_pairs, 8)


def cmd_e1_resilience(args):
    """What breaks when random parts of the network fail, and does it matter
    which layer fails. Part A: connectivity + inter-pod path diversity vs.
    link failure %, one curve per k, with theory overlays. Part B: % of
    server pairs disconnected when one switch layer at a time fails, k fixed.
    """
    rows_conn = []
    for k in args.ks:
        g = build_fat_tree(k)
        pod_pairs = _e1_pod_pairs(k, args.seed)
        for pct in args.pcts:
            fc_vals, pd_vals = [], []
            for t in range(args.trials):
                failures_cfg = {
                    "link_pct": pct, "switch_pct": 0,
                    "link_layers": ["edge-agg", "agg-core"], "switch_layers": [], "mode": "bernoulli",
                }
                g2, _ = apply_failures(g, failures_cfg, seed=args.seed + t)
                fc_vals.append(connectivity_and_path_metrics(g, g2)["fraction_connected"])
                pd_vals.append(sum(path_diversity(g2, k, pa, pb) for pa, pb in pod_pairs) / len(pod_pairs))
            fc_mean, fc_ci = _mean_ci95(fc_vals)
            pd_mean, pd_ci = _mean_ci95(pd_vals)
            p = pct / 100
            rows_conn.append({
                "k": k, "link_pct": pct,
                "fraction_connected_mean": fc_mean, "fraction_connected_ci95": fc_ci,
                "theory_connected": 1 - 2 * p ** (k // 2),
                "path_diversity_mean": pd_mean, "path_diversity_ci95": pd_ci,
                "theory_diversity": (1 - p) ** 4,
            })
            print(f"k={k:>2} link_pct={pct:>4}%  connected={fc_mean:.4f} (theory {1 - 2 * p ** (k // 2):.4f})  "
                  f"diversity={pd_mean:.3f} (theory {(1 - p) ** 4:.3f})")

    rows_layer = []
    g_layer = build_fat_tree(args.layer_k)
    for layer in ("edge", "agg", "core"):
        for pct in args.switch_pcts:
            vals = []
            for t in range(args.trials):
                failures_cfg = {
                    "link_pct": 0, "switch_pct": pct,
                    "link_layers": [], "switch_layers": [layer], "mode": "bernoulli",
                }
                g2, _ = apply_failures(g_layer, failures_cfg, seed=args.seed + t)
                vals.append(connectivity_and_path_metrics(g_layer, g2)["fraction_connected"])
            mean, ci = _mean_ci95(vals)
            rows_layer.append({
                "layer": layer, "switch_pct": pct,
                "pct_disconnected_mean": 100 * (1 - mean), "pct_disconnected_ci95": 100 * ci,
            })
            print(f"layer={layer:<5} switch_pct={pct}%  disconnected={100 * (1 - mean):.2f}% (+/-{100 * ci:.2f})")

    _write_csv(args.csv, rows_conn)
    _write_csv(args.csv_layer, rows_layer)
    print(f"wrote {plot_e1_connectivity(rows_conn, args.ks, args.plot_connectivity)}")
    print(f"wrote {plot_e1_path_diversity(rows_conn, args.ks, args.plot_diversity)}")
    print(f"wrote {plot_e1_layer(rows_layer, args.plot_layer)}")


# --- e2-throughput (Phase 4, PHASE4.md Step 5) --------------------------------------------------------------------

E2_SCHEMES = ["two-level", "two-level-failover", "ecmp"]


def _e2_router(scheme, g, cfg):
    real_scheme = "ecmp" if scheme == "ecmp" else "two-level"
    if scheme == "two-level-failover":
        cfg["two_level"]["failover"] = True
    cfg["routing"] = real_scheme
    return get_router(real_scheme, g, cfg)


def _e2_one_trial(g, scheme, k, pct, flow_size_bytes, link_capacity_gbps, seed):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["topology"]["k"] = k
    cfg["traffic"]["pattern"] = "random"
    cfg["traffic"]["load"] = 1.0  # every host sends at line rate
    cfg["traffic"]["size_model"] = "fixed"
    cfg["traffic"]["fixed_size_bytes"] = flow_size_bytes
    cfg["failures"] = {"link_pct": pct, "switch_pct": 0,
                        "link_layers": ["edge-agg", "agg-core"], "switch_layers": [], "mode": "bernoulli"}

    g_after, failed = apply_failures(g, cfg["failures"], seed=seed)
    router = _e2_router(scheme, g_after, cfg)
    router.on_failure(failed["failed_links"], failed["failed_switches"])
    flows = generate_traffic(g, cfg, seed=seed + 1000)
    result = simulate_flows(router, flows, link_capacity_gbps, cfg["traffic"]["load"])
    return result["throughput_pct_of_ideal"]


def cmd_e2_throughput(args):
    """How much of full speed servers really get, with/without failures, and
    how routing choice changes that -- plus, at 0% failures, how much ECMP's
    per-flow hashing alone costs vs. the paper's Sec. 5.3 expectation."""
    rows_fail = []
    g = build_fat_tree(args.k)
    for scheme in E2_SCHEMES:
        for pct in args.pcts:
            vals = [_e2_one_trial(g, scheme, args.k, pct, args.flow_size_bytes, args.link_capacity_gbps, args.seed + t)
                    for t in range(args.trials)]
            mean, ci95 = _mean_ci95(vals)
            rows_fail.append({"scheme": scheme, "k": args.k, "link_pct": pct, "mean_pct": mean, "ci95": ci95})
            print(f"{scheme:<18} k={args.k:>2} link_pct={pct:>4}%  throughput={mean:6.1f}% (+/- {ci95:.1f})")

    rows_k = []
    for scheme in E2_SCHEMES:
        for k in args.ks:
            g_k = build_fat_tree(k)
            vals = [_e2_one_trial(g_k, scheme, k, 0, args.flow_size_bytes, args.link_capacity_gbps, args.seed + t)
                    for t in range(args.trials)]
            mean, ci95 = _mean_ci95(vals)
            theory = 100 * (1 - (1 - 2 / k) ** (k // 2))
            rows_k.append({"scheme": scheme, "k": k, "mean_pct": mean, "ci95": ci95, "theory_pct": theory})
            print(f"{scheme:<18} k={k:>2} (0%% failures)  throughput={mean:6.1f}%% (+/- {ci95:.1f})  theory={theory:.1f}%%")

    _write_csv(args.csv_failures, rows_fail)
    _write_csv(args.csv_collision, rows_k)
    print(f"wrote {plot_e2_vs_failures(rows_fail, E2_SCHEMES, args.plot_failures)}")
    print(f"wrote {plot_e2_collision_ceiling(rows_k, E2_SCHEMES, args.plot_collision)}")


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


# --- incast (Experiment 9, PHASE3_2_EXP9_INCAST.md) --------------------------------------------------------------------

def _round_time_and_unfinished(flow_stats, duration_ns):
    """A round's wall time and straggler count, given its flows' FlowStats
    and the simulation's time budget. If *any* flow in the round didn't
    finish, the round isn't over once the others do -- that would undercount
    its real time (PHASE6.md Step 1: a round with an unfinished flow was
    timed by its finished flows only, making it look faster than it was,
    which could even read as a physically impossible >=100% goodput). Such a
    round is charged the full simulated budget instead."""
    unfinished = sum(1 for fs in flow_stats.values() if fs.fct_ns() is None)
    if unfinished > 0:
        return duration_ns, unfinished
    return max(fs.fct_ns() for fs in flow_stats.values()), 0


def _run_incast_rounds(g, router, cfg, m, rounds, seed):
    """Run `rounds` sequential, barrier-synchronized incast rounds: round r+1's
    M requests start only once round r's M responses have all finished (or the
    round's own packet.duration_ms budget runs out, to bound collapse cases).
    Each round is simulated as an independent flow set with fresh TCP/queue
    state (not one continuous connection reused across rounds) -- a reasonable
    model for short-lived RPC-style incast request/response bursts, the
    classic incast benchmark setup. Each sender's request starts at a small
    random offset (`incast.start_jitter_us`, seeded) rather than all exactly
    at t=0, so the burst isn't an unrealistically perfect lockstep collision.

    Returns aggregate goodput, drop locations, timeouts, and round-time stats.
    """
    cfg = copy.deepcopy(cfg)
    cfg["traffic"]["pattern"] = "incast"
    cfg["traffic"]["incast_senders"] = m
    block_bytes = cfg["incast"]["block_bytes"]
    mtu = cfg["packet"]["mtu_bytes"]
    start_jitter_s = cfg["incast"].get("start_jitter_us", 5) * 1e-6
    receiver = cfg["incast"].get("receiver") or hosts_of(g)[0]
    receiver_edge = next((n for n in g.neighbors(receiver) if g.nodes[n]["type"] == "edge"), None)

    total_bytes = 0
    total_time_ns = 0
    total_timeouts = 0
    total_fast_retx = 0
    total_unfinished = 0
    n_senders = None
    drops_by_layer = {"host": 0, "edge": 0, "agg": 0, "core": 0}
    receiver_drops = 0
    max_receiver_queue = 0
    round_times_ns = []
    receiver_queue_series = None    # (time_ns, len) samples for one representative round
    receiver_drop_times_ns = None   # drop timestamps at the receiver's edge port, same round

    for r in range(rounds):
        pairs = generate_pairs(g, cfg, seed=seed + r)
        n_senders = len(pairs)  # same every round (same m/placement/receiver); PHASE6.md Step 1
        jitter_rng = random.Random(seed + r + 500_000)
        flows = [Flow(src, dst, block_bytes, jitter_rng.uniform(0, start_jitter_s), "mouse") for src, dst in pairs]
        sim = PacketSimulator(g, router, flows, cfg)
        result = sim.run()

        round_time_ns, unfinished_this_round = _round_time_and_unfinished(result["flow_stats"], sim.duration_ns)
        total_unfinished += unfinished_this_round
        round_times_ns.append(round_time_ns)
        total_time_ns += round_time_ns

        for idx, fs in result["flow_stats"].items():
            total_timeouts += fs.timeout_count
            total_fast_retx += fs.fast_retransmit_count
            if fs.fct_ns() is not None:
                total_bytes += fs.size_bytes
            elif idx in sim.receivers:
                total_bytes += min(fs.size_bytes, sim.receivers[idx].expected_seq * mtu)

        for (u, v), ps in result["port_stats"].items():
            if ps.drops <= 0:
                continue
            layer = g.nodes[v]["type"] if g.has_node(v) else "host"
            drops_by_layer[layer] = drops_by_layer.get(layer, 0) + ps.drops
            if v == receiver:
                receiver_drops += ps.drops

        if receiver_edge is not None:
            key = (receiver_edge, receiver)
            if key in result["port_stats"]:
                max_receiver_queue = max(max_receiver_queue, result["port_stats"][key].max_queue_len())
                if r == rounds // 2:
                    receiver_queue_series = list(result["port_stats"][key].queue_len_samples)
                    receiver_drop_times_ns = list(result["port_stats"][key].drop_times_ns)

    round_times_ns.sort()
    p99_round_ns = round_times_ns[min(len(round_times_ns) - 1, int(round(0.99 * (len(round_times_ns) - 1))))]

    return {
        "goodput_bps": (total_bytes * 8 / (total_time_ns / 1e9)) if total_time_ns else 0.0,
        "total_bytes": total_bytes,
        "timeouts_per_round": total_timeouts / rounds,
        "fast_retx_per_round": total_fast_retx / rounds,
        "drops_by_layer": drops_by_layer,
        "receiver_drops": receiver_drops,
        "max_receiver_queue": max_receiver_queue,
        "mean_round_time_ns": sum(round_times_ns) / len(round_times_ns),
        "p99_round_time_ns": p99_round_ns,
        "receiver_queue_series": receiver_queue_series,
        "receiver_drop_times_ns": receiver_drop_times_ns,
        "unfinished_flows": total_unfinished,
        "n_senders": n_senders,
    }


def cmd_incast(args):
    k = args.k
    g = build_fat_tree(k)
    g_tree = build_traditional_tree(k)

    base_cfg = copy.deepcopy(DEFAULTS)
    base_cfg["topology"]["k"] = k
    base_cfg["routing"] = "two-level"
    base_cfg["packet"]["transport"] = "tcp_lite"
    base_cfg["packet"]["duration_ms"] = args.duration_ms
    base_cfg["incast"]["block_bytes"] = args.block_bytes
    receiver_link_gbps = base_cfg["link_capacity_gbps"]

    all_rows = []

    def sweep(label, topology_name, g_use, queue_size, min_rto_ms, csv_suffix):
        router_name = "two-level" if topology_name == "fat-tree" else "single-path"
        rows = []
        for m in args.ms:
            goodputs = []
            extra = None
            for t in range(args.trials):
                cfg = copy.deepcopy(base_cfg)
                cfg["packet"]["queue_size_packets"] = queue_size
                cfg["packet"]["min_rto_ms"] = min_rto_ms
                router = get_router(router_name, g_use, cfg)
                stats = _run_incast_rounds(g_use, router, cfg, m, args.rounds, seed=args.seed + t * 1000)
                goodputs.append(stats["goodput_bps"])
                extra = stats  # keep the last trial's detail for drop/queue figures
            mean, ci95 = _mean_ci95(goodputs)
            row = {
                "label": label, "topology": topology_name, "queue_size_packets": queue_size,
                "min_rto_ms": min_rto_ms, "m": m,
                "goodput_pct_of_line_rate": 100 * mean / receiver_link_gbps / 1e9,
                "goodput_ci95_pct": 100 * ci95 / receiver_link_gbps / 1e9,
                "timeouts_per_round": extra["timeouts_per_round"],
                "receiver_drops": extra["receiver_drops"],
                "max_receiver_queue": extra["max_receiver_queue"],
                "p99_round_time_ms": extra["p99_round_time_ns"] / 1e6,
            }
            row["_drops_by_layer"] = extra["drops_by_layer"]
            row["_queue_series"] = extra["receiver_queue_series"]
            rows.append(row)
            print(f"{label:<22} M={m:>3}  goodput={row['goodput_pct_of_line_rate']:5.1f}% "
                  f"(+/-{row['goodput_ci95_pct']:.1f})  timeouts/round={row['timeouts_per_round']:.2f}  "
                  f"recv_drops={row['receiver_drops']}")
        return rows

    # Figure 1: goodput vs M, one curve per buffer size (min RTO 200ms, fat-tree)
    buffer_rows = []
    for q in args.queue_sizes:
        buffer_rows += sweep(f"buffer={q}", "fat-tree", g, q, 200, "buffer")
    all_rows += buffer_rows

    # Figure 2: goodput vs M, min RTO 200ms vs 1ms (buffer = args.queue_sizes[-1])
    rto_q = args.queue_sizes[len(args.queue_sizes) // 2]
    rto_rows = []
    for rto in [200, 1]:
        rto_rows += sweep(f"min_rto_ms={rto}", "fat-tree", g, rto_q, rto, "rto")
    all_rows += rto_rows

    # Figure 3 (+4): goodput vs M, fat-tree vs traditional tree (buffer=rto_q, min RTO 200ms)
    topo_rows = []
    topo_rows += sweep("fat-tree", "fat-tree", g, rto_q, 200, "topo")
    topo_rows += sweep("traditional-tree", "traditional-tree", g_tree, rto_q, 200, "topo")
    all_rows += topo_rows

    csv_rows = [{key: v for key, v in row.items() if not key.startswith("_")} for row in all_rows]
    _write_csv(args.csv, csv_rows)

    print(f"wrote {plot_incast_goodput_vs_m(buffer_rows, 'queue_size_packets', 'buffer', args.plot_buffer)}")
    print(f"wrote {plot_incast_goodput_vs_m(rto_rows, 'min_rto_ms', 'min RTO (ms)', args.plot_rto)}")
    print(f"wrote {plot_incast_goodput_vs_m(topo_rows, 'topology', 'topology', args.plot_topo)}")
    print(f"wrote {plot_incast_drop_location(topo_rows, args.plot_drops)}")
    print(f"wrote {plot_incast_timeouts_vs_m(buffer_rows, args.plot_timeouts)}")

    series_row = next((r for r in topo_rows if r["topology"] == "fat-tree" and r["m"] == args.ms[-1]), None)
    if series_row and series_row["_queue_series"]:
        print(f"wrote {plot_incast_queue_series(series_row['_queue_series'], args.ms[-1], args.plot_queue)}")


# --- e3-incast (Experiment 3, "smart": Incast -- PHASE4.md Step 6) --------------------------------------------------------------------

def _e3_base_cfg(k, block_bytes, rounds, duration_ms, queue_size, min_rto_ms):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["topology"]["k"] = k
    cfg["routing"] = "two-level"
    cfg["packet"]["transport"] = "tcp_lite"
    cfg["packet"]["queue_size_packets"] = queue_size
    cfg["packet"]["min_rto_ms"] = min_rto_ms
    cfg["packet"]["duration_ms"] = duration_ms
    cfg["incast"]["block_bytes"] = block_bytes
    cfg["incast"]["rounds"] = rounds
    return cfg


def cmd_e3_incast(args):
    """Why throughput collapses when many servers answer one at once, what
    controls the collapse point, and whether the fat-tree's full bisection
    bandwidth helps (it doesn't: the bottleneck is the receiver's own last
    cable, which no amount of core capacity widens)."""
    k = args.k
    g = build_fat_tree(k)
    cap_bps = DEFAULTS["link_capacity_gbps"] * 1e9

    # --- Figure 1: collapse vs M, one line per buffer size, min RTO 200ms ---
    collapse_rows = []
    for q in args.queue_sizes:
        for m in args.ms:
            cfg = _e3_base_cfg(k, args.block_bytes, args.rounds, args.duration_ms, q, 200)
            router = get_router("two-level", g, cfg)
            trial_stats = [_run_incast_rounds(g, router, cfg, m, args.rounds, seed=args.seed + t * 1000)
                           for t in range(args.trials)]
            goodputs = [s["goodput_bps"] for s in trial_stats]
            unfinished = sum(s["unfinished_flows"] for s in trial_stats)
            mean, ci95 = _mean_ci95(goodputs)
            collapse_rows.append({"queue_size_packets": q, "m": m,
                                   "goodput_pct": 100 * mean / cap_bps, "goodput_ci95_pct": 100 * ci95 / cap_bps,
                                   "unfinished_flows": unfinished})
            print(f"buffer={q:>3} M={m:>3}  goodput={100 * mean / cap_bps:5.1f}% (+/-{100 * ci95 / cap_bps:.1f})"
                  f"{'  [unfinished=' + str(unfinished) + ']' if unfinished else ''}")

    # --- Figure 2: where time goes, one buffer size, min RTO 200ms vs 1ms ---
    time_rows = []
    for min_rto in (200, 1):
        for m in args.ms:
            cfg = _e3_base_cfg(k, args.block_bytes, args.rounds, args.duration_ms, args.time_buffer, min_rto)
            router = get_router("two-level", g, cfg)
            trial_stats = [_run_incast_rounds(g, router, cfg, m, args.rounds, seed=args.seed + t * 1000)
                           for t in range(args.trials)]
            mean_round_ns = sum(s["mean_round_time_ns"] for s in trial_stats) / len(trial_stats)
            unfinished = sum(s["unfinished_flows"] for s in trial_stats)
            n_senders = trial_stats[0]["n_senders"]  # same every trial (same m/placement/receiver)
            # Ideal sending time uses the senders actually created, not the
            # requested m (PHASE6.md Step 1): generate_pairs now raises
            # rather than silently creating fewer, but deriving this from the
            # real flow count is the more robust fix either way.
            ideal_send_ns = n_senders * args.block_bytes * 8 * 1e9 / cap_bps
            waiting_ns = mean_round_ns - ideal_send_ns
            if waiting_ns < 0:
                raise RuntimeError(
                    f"negative waiting time ({waiting_ns / 1e6:.3f}ms) at min_rto_ms={min_rto} M={m} "
                    f"(n_senders={n_senders}): measurement bug, not a real result"
                )
            time_rows.append({"min_rto_ms": min_rto, "m": m,
                               "sending_ms": ideal_send_ns / 1e6, "waiting_ms": waiting_ns / 1e6,
                               "unfinished_flows": unfinished})
            print(f"min_rto_ms={min_rto:>3} M={m:>3}  sending={ideal_send_ns / 1e6:.2f}ms waiting={waiting_ns / 1e6:.2f}ms"
                  f"{'  [unfinished=' + str(unfinished) + ']' if unfinished else ''}")

    # --- Figure 3: queue trace, M=2 vs M=8, one buffer size, min RTO 200ms ---
    queue_m_values = [m for m in (2, 8) if m in args.ms] or args.ms[:2]
    queue_traces = {}
    for m in queue_m_values:
        cfg = _e3_base_cfg(k, args.block_bytes, max(args.rounds, 3), args.duration_ms, args.time_buffer, 200)
        router = get_router("two-level", g, cfg)
        stats = _run_incast_rounds(g, router, cfg, m, max(args.rounds, 3), seed=args.seed)
        queue_traces[m] = (stats["receiver_queue_series"], stats["receiver_drop_times_ns"])

    # inset: fat-tree vs. traditional-tree goodput at one M, same buffer/RTO
    g_tree = build_traditional_tree(k)
    topo_goodput = {}
    for name, g_use, router_name in (("fat-tree", g, "two-level"), ("traditional-tree", g_tree, "single-path")):
        cfg = _e3_base_cfg(k, args.block_bytes, args.rounds, args.duration_ms, args.time_buffer, 200)
        router = get_router(router_name, g_use, cfg)
        vals = [_run_incast_rounds(g_use, router, cfg, args.compare_m, args.rounds, seed=args.seed + t * 1000)["goodput_bps"]
                for t in range(args.trials)]
        mean, _ = _mean_ci95(vals)
        topo_goodput[name] = 100 * mean / cap_bps
        print(f"{name:<18} M={args.compare_m}  goodput={topo_goodput[name]:5.1f}%")

    _write_csv(args.csv_collapse, collapse_rows)
    _write_csv(args.csv_time, time_rows)
    print(f"wrote {plot_e3_collapse(collapse_rows, args.ms, args.plot_collapse)}")
    print(f"wrote {plot_e3_where_time_goes(time_rows, args.ms, args.plot_time)}")
    print(f"wrote {plot_e3_queue(queue_traces, args.time_buffer, topo_goodput, args.compare_m, args.plot_queue)}")


# --- mice-elephants (Experiment 7, PHASE3_3_EXP7_MICE_ELEPHANTS.md Part A) --------------------------------------------------------------------

def _analytic_ideal_fct_ns(path, size_bytes, mtu_bytes, capacity_bps, prop_delay_ns):
    """FCT for `size_bytes` alone on `path` with no contention: the first
    packet needs the full hop-by-hop (transmission + propagation) delay, and
    the rest pipeline in behind it at the per-hop transmission rate. Used as
    the "ideal" baseline for slowdown instead of a second full simulation per
    flow (hundreds of flows per trial makes that too slow) -- a documented
    simplification: it ignores tcp_lite's slow-start ramp-up, so it slightly
    *underestimates* ideal FCT for very small mice where RTT dominates.
    """
    if path is None:
        return None
    n_hops = len(path) - 1
    if n_hops <= 0:
        return 0
    n_packets = max(1, -(-int(size_bytes) // mtu_bytes))
    sizes = [mtu_bytes] * (n_packets - 1) + [size_bytes - (n_packets - 1) * mtu_bytes]
    tx_times_ns = [int(s * 8 * 1e9 / capacity_bps) for s in sizes]
    return n_hops * (tx_times_ns[0] + prop_delay_ns) + sum(tx_times_ns[1:])


def _precompute_router(scheme, g, cfg, flows):
    """Router for `scheme`, usable by the packet simulator's one-route(flow)-
    call-per-flow interface. classifier and scheduler are precomputed in
    arrival order over the whole batch -- see fattree/routing/precomputed.py's
    docstring for why (a documented simplification vs. live mid-flow
    rearrangement). The rest route live and need no precomputation."""
    if scheme == "classifier":
        router = get_router("classifier", g, cfg)
        paths = router.route_all(flows)
        return PrecomputedPathRouter({id(f): p for f, p in zip(flows, paths)}, stats=router.stats())
    if scheme == "scheduler":
        # Arrival order, with reservations released once their *estimated*
        # (analytic, uncongested) completion time has passed -- true release
        # would need to know actual completion times, which depend on the
        # simulation this router is about to drive (circular). At k=4 there
        # are only (k/2)^2=4 inter-pod core paths / k/2=2 intra-pod agg paths
        # total, so never releasing at all (as a literal one-shot batch
        # precompute would) exhausts them after just a few elephants and
        # starves every later one of a path entirely -- this estimate is a
        # documented approximation, not the real thing.
        router = get_router("scheduler", g, cfg)
        mtu = cfg["packet"]["mtu_bytes"]
        cap_bps = cfg["link_capacity_gbps"] * 1e9
        prop_ns = cfg["packet"]["propagation_delay_ns"]
        paths_by_id = {}
        pending = []  # (estimated_release_time_s, flow)
        for f in sorted(flows, key=lambda fl: fl.start_time):
            still_pending = []
            for release_time, pending_flow in pending:
                if release_time <= f.start_time:
                    router.release(pending_flow)
                else:
                    still_pending.append((release_time, pending_flow))
            pending = still_pending

            path = router.route(f)
            paths_by_id[id(f)] = path
            if path is not None:
                ideal_ns = _analytic_ideal_fct_ns(path, f.size_bytes, mtu, cap_bps, prop_ns)
                pending.append((f.start_time + ideal_ns / 1e9, f))
        return PrecomputedPathRouter(paths_by_id, stats=router.stats())
    return get_router(scheme, g, cfg)


def cmd_mice_elephants(args):
    k = args.k
    g = build_fat_tree(k)
    schemes = ["single-path", "ecmp", "two-level", "classifier", "scheduler"]
    # Poisson arrivals spread across the whole generation window, so some flows
    # (especially multi-MB elephants) land close to any hard cutoff with no
    # real chance to finish -- not congestion, just an edge-of-window artifact.
    # Fix: only *measure* flows starting in [warmup, warmup+duration), but let
    # the simulation itself run `tail_ms` longer so those flows get a fair
    # chance to complete even if they start right at the end of that window.
    warmup_s = args.warmup_ms / 1000
    measure_end_s = (args.warmup_ms + args.duration_ms) / 1000
    sim_duration_s = (args.warmup_ms + args.duration_ms + args.tail_ms) / 1000

    rows = []
    cdf_data = {}  # (load, scheme) -> mouse FCT ns list, load=0.7 trial 0 (figure 5)
    bar_data = {}  # scheme -> (mouse_slowdown_mean, elephant_slowdown_mean) at load=0.5 (figure 1)
    p99_bar_data = {}  # scheme -> mouse p99 slowdown at load=0.5 (figure 2)
    byte_share = {"mouse": 0, "elephant": 0}
    flow_share = {"mouse": 0, "elephant": 0}

    for load in args.loads:
        for scheme in schemes:
            mouse_fcts_ns, elephant_fcts_ns = [], []
            mouse_slowdowns, elephant_slowdowns = [], []
            elephant_goodputs = []
            mouse_unfinished = elephant_unfinished = 0
            mouse_flows_n = elephant_flows_n = 0
            extra_stats = {}

            for t in range(args.trials):
                cfg = copy.deepcopy(DEFAULTS)
                cfg["topology"]["k"] = k
                cfg["routing"] = "two-level" if scheme in ("classifier", "scheduler") else scheme
                cfg["traffic"]["load"] = load
                cfg["traffic"]["size_model"] = "mice-elephants"
                cfg["traffic"]["elephant_fraction"] = args.elephant_fraction
                cfg["traffic"]["mouse_size_range"] = [2_000, 100_000]
                cfg["traffic"]["elephant_size_range"] = [1_000_000, 10_000_000]
                cfg["traffic"]["elephant_threshold_bytes"] = args.elephant_threshold_bytes
                cfg["packet"]["transport"] = "tcp_lite"
                cfg["packet"]["min_rto_ms"] = 1
                cfg["packet"]["dupack_threshold"] = 3
                cfg["packet"]["queue_size_packets"] = 100
                cfg["packet"]["duration_ms"] = args.warmup_ms + args.duration_ms + args.tail_ms
                cfg["classifier"]["rearrange_interval_s"] = 0.01

                flows = generate_poisson_flows(g, cfg, sim_duration_s, seed=args.seed + t)
                router = _precompute_router(scheme, g, cfg, flows)
                result = simulate_packets(g, router, flows, cfg)
                extra_stats = router.stats()

                mtu = cfg["packet"]["mtu_bytes"]
                cap_bps = cfg["link_capacity_gbps"] * 1e9
                prop_ns = cfg["packet"]["propagation_delay_ns"]
                trial_mouse_fcts = []

                if load == args.loads[0] and t == 0:
                    measured_flows = [f for f in flows if warmup_s <= f.start_time < measure_end_s]
                    offered = sum(f.size_bytes for f in measured_flows) / (measure_end_s - warmup_s)
                    target = load * cap_bps / 8 * len(hosts_of(g))  # network-wide, not per-host
                    print(f"  [offered-load check] scheme={scheme} load={load}: "
                          f"measured={offered / 1e6:.1f} MB/s target={target / 1e6:.1f} MB/s "
                          f"({100 * offered / target:.1f}%)")

                for idx, flow in enumerate(flows):
                    if not (warmup_s <= flow.start_time < measure_end_s):
                        continue
                    is_mouse = flow.cls == "mouse"
                    if is_mouse:
                        mouse_flows_n += 1
                    else:
                        elephant_flows_n += 1
                    byte_share[flow.cls] += flow.size_bytes
                    flow_share[flow.cls] += 1

                    fs = result["flow_stats"][idx]
                    if fs.fct_ns() is None:
                        if is_mouse:
                            mouse_unfinished += 1
                        else:
                            elephant_unfinished += 1
                        continue

                    path = router.route(flow)
                    ideal_ns = _analytic_ideal_fct_ns(path, flow.size_bytes, mtu, cap_bps, prop_ns)
                    sd = (fs.fct_ns() / ideal_ns) if ideal_ns else None

                    if is_mouse:
                        mouse_fcts_ns.append(fs.fct_ns())
                        trial_mouse_fcts.append(fs.fct_ns())
                        if sd is not None:
                            mouse_slowdowns.append(sd)
                    else:
                        elephant_fcts_ns.append(fs.fct_ns())
                        elephant_goodputs.append(fs.goodput_bps())
                        if sd is not None:
                            elephant_slowdowns.append(sd)

                if load == 0.7 and t == 0:
                    cdf_data[scheme] = sorted(trial_mouse_fcts)

            mouse_mean, mouse_ci = _mean_ci95(mouse_slowdowns) if mouse_slowdowns else (None, None)
            eleph_mean, eleph_ci = _mean_ci95(elephant_slowdowns) if elephant_slowdowns else (None, None)
            mouse_p99 = sorted(mouse_slowdowns)[int(0.99 * (len(mouse_slowdowns) - 1))] if mouse_slowdowns else None
            eleph_goodput_mean, eleph_goodput_ci = (
                _mean_ci95(elephant_goodputs) if elephant_goodputs else (None, None))

            row = {
                "load": load, "scheme": scheme,
                "mouse_slowdown_mean": mouse_mean, "mouse_slowdown_ci95": mouse_ci,
                "mouse_slowdown_p99": mouse_p99,
                "elephant_slowdown_mean": eleph_mean, "elephant_slowdown_ci95": eleph_ci,
                "elephant_goodput_mean_bps": eleph_goodput_mean, "elephant_goodput_ci95_bps": eleph_goodput_ci,
                "mouse_unfinished": mouse_unfinished, "elephant_unfinished": elephant_unfinished,
                "mouse_flows_n": mouse_flows_n, "elephant_flows_n": elephant_flows_n,
                "rearrangements": extra_stats.get("rearrangements"),
                "placements": extra_stats.get("placements"),
                "mean_placement_time_s": extra_stats.get("mean_placement_time_s"),
            }
            rows.append(row)
            print(f"load={load} {scheme:<12} mouse_slowdown={mouse_mean} (p99={mouse_p99}) "
                  f"elephant_slowdown={eleph_mean} elephant_goodput={eleph_goodput_mean} "
                  f"unfinished(m/e)={mouse_unfinished}/{elephant_unfinished}")

            if load == 0.5:
                bar_data[scheme] = (mouse_mean, eleph_mean)
                p99_bar_data[scheme] = mouse_p99

    _write_csv(args.csv, rows)
    print(f"wrote {plot_mice_elephants_bars(bar_data, args.plot_bars)}")
    print(f"wrote {plot_mice_elephants_bars({s: (p, None) for s, p in p99_bar_data.items()}, args.plot_p99_bars, ylabel='mouse p99 slowdown', title='Mouse tail latency (p99 slowdown) by scheme, load=0.5', single=True)}")
    print(f"wrote {plot_mice_elephants_line(rows, schemes, 'mouse_slowdown_p99', 'mouse p99 slowdown', args.plot_mouse_line)}")
    print(f"wrote {plot_mice_elephants_line(rows, schemes, 'elephant_goodput_mean_bps', 'elephant goodput (bps)', args.plot_elephant_line)}")
    print(f"wrote {plot_mice_elephants_mouse_fct_cdf(cdf_data, args.plot_cdf)}")
    print(f"wrote {plot_mice_elephants_bytes_pie(flow_share, byte_share, args.plot_pie)}")


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

    pe1 = sub.add_parser("e1-resilience", help="E1: resilience -- what breaks, and how much it matters (Phase 4)")
    pe1.add_argument("--ks", type=int, nargs="+", default=[8, 16, 32])
    pe1.add_argument("--pcts", type=float, nargs="+", default=[0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60])
    pe1.add_argument("--trials", type=int, default=30)
    pe1.add_argument("--seed", type=int, default=1)
    pe1.add_argument("--layer-k", type=int, default=16)
    pe1.add_argument("--switch-pcts", type=float, nargs="+", default=[5, 10])
    pe1.add_argument("--csv", default="results/e1_connectivity.csv")
    pe1.add_argument("--csv-layer", default="results/e1_layer.csv")
    pe1.add_argument("--plot-connectivity", default="figures/e1_connectivity.png")
    pe1.add_argument("--plot-diversity", default="figures/e1_path_diversity.png")
    pe1.add_argument("--plot-layer", default="figures/e1_layer.png")
    pe1.set_defaults(func=cmd_e1_resilience)

    pe2 = sub.add_parser("e2-throughput", help="E2: speed under failures, and the hash-collision ceiling (Phase 4)")
    pe2.add_argument("--k", type=int, default=16, help="k for the vs.-failures sweep (Part A)")
    pe2.add_argument("--pcts", type=float, nargs="+", default=[0, 5, 10, 15, 20, 25, 30])
    pe2.add_argument("--ks", type=int, nargs="+", default=[4, 8, 16, 24, 32], help="k values for the vs.-k sweep (Part B)")
    pe2.add_argument("--trials", type=int, default=20)
    pe2.add_argument("--seed", type=int, default=1)
    pe2.add_argument("--flow-size-bytes", type=int, default=1_000_000)
    pe2.add_argument("--link-capacity-gbps", type=float, default=1.0)
    pe2.add_argument("--csv-failures", default="results/e2_vs_failures.csv")
    pe2.add_argument("--csv-collision", default="results/e2_collision_ceiling.csv")
    pe2.add_argument("--plot-failures", default="figures/e2_vs_failures.png")
    pe2.add_argument("--plot-collision", default="figures/e2_collision_ceiling.png")
    pe2.set_defaults(func=cmd_e2_throughput)

    p5 = sub.add_parser("link-vs-switch", help="link failures vs switch failures at equal %%: connectivity + path diversity")
    p5.add_argument("--k", type=int, default=16)
    p5.add_argument("--pcts", type=float, nargs="+", default=[0, 1, 2, 5, 10, 15, 20])
    p5.add_argument("--trials", type=int, default=15)
    p5.add_argument("--seed", type=int, default=1)
    p5.add_argument("--csv", default="results/link_vs_switch_failures.csv")
    p5.add_argument("--plot", default="figures/link_vs_switch_failures.png")
    p5.set_defaults(func=cmd_link_vs_switch)

    p6 = sub.add_parser("incast", help="Experiment 9: incast goodput collapse vs number of senders M")
    p6.add_argument("--k", type=int, default=4)
    p6.add_argument("--ms", type=int, nargs="+", default=[1, 2, 4, 8, 15])
    p6.add_argument("--queue-sizes", type=int, nargs="+", default=[32, 64, 128])
    p6.add_argument("--rounds", type=int, default=10)
    p6.add_argument("--trials", type=int, default=5)
    p6.add_argument("--seed", type=int, default=1)
    p6.add_argument("--block-bytes", type=int, default=65_536)
    p6.add_argument("--duration-ms", type=float, default=3000)
    p6.add_argument("--csv", default="results/incast.csv")
    p6.add_argument("--plot-buffer", default="figures/incast_goodput_vs_m_buffer.png")
    p6.add_argument("--plot-rto", default="figures/incast_goodput_vs_m_rto.png")
    p6.add_argument("--plot-topo", default="figures/incast_goodput_vs_m_topology.png")
    p6.add_argument("--plot-drops", default="figures/incast_drop_location.png")
    p6.add_argument("--plot-timeouts", default="figures/incast_timeouts_vs_m.png")
    p6.add_argument("--plot-queue", default="figures/incast_queue_series.png")
    p6.set_defaults(func=cmd_incast)

    pe3 = sub.add_parser("e3-incast", help="E3: incast -- collapse point, where time goes, queue dynamics (Phase 4)")
    pe3.add_argument("--k", type=int, default=4)
    # 12, not 15: k=4's default sender_placement (other-pods) only has 12
    # hosts outside the receiver's pod -- requesting more now raises
    # (PHASE6.md Step 1) instead of silently creating fewer senders than asked.
    pe3.add_argument("--ms", type=int, nargs="+", default=[1, 2, 4, 8, 12])
    pe3.add_argument("--queue-sizes", type=int, nargs="+", default=[32, 64, 128])
    pe3.add_argument("--time-buffer", type=int, default=64, help="buffer size (packets) used for figures 2 and 3")
    pe3.add_argument("--compare-m", type=int, default=8, help="M used for the fat-tree vs. traditional-tree comparison")
    pe3.add_argument("--rounds", type=int, default=20)
    pe3.add_argument("--trials", type=int, default=10)
    pe3.add_argument("--seed", type=int, default=1)
    pe3.add_argument("--block-bytes", type=int, default=65_536)
    pe3.add_argument("--duration-ms", type=float, default=3000)
    pe3.add_argument("--csv-collapse", default="results/e3_collapse.csv")
    pe3.add_argument("--csv-time", default="results/e3_where_time_goes.csv")
    pe3.add_argument("--plot-collapse", default="figures/e3_collapse.png")
    pe3.add_argument("--plot-time", default="figures/e3_where_time_goes.png")
    pe3.add_argument("--plot-queue", default="figures/e3_queue.png")
    pe3.set_defaults(func=cmd_e3_incast)

    p7 = sub.add_parser("mice-elephants", help="Experiment 7: FCT/slowdown of mice vs elephants, by routing scheme")
    p7.add_argument("--k", type=int, default=4)
    p7.add_argument("--loads", type=float, nargs="+", default=[0.3, 0.5, 0.7])
    p7.add_argument("--duration-ms", type=float, default=200)
    p7.add_argument("--warmup-ms", type=float, default=20)
    p7.add_argument("--tail-ms", type=float, default=300,
                    help="extra simulated time past the measurement window so late-arriving "
                         "elephants have a fair chance to finish (not itself measured)")
    p7.add_argument("--trials", type=int, default=5)
    p7.add_argument("--seed", type=int, default=1)
    p7.add_argument("--elephant-fraction", type=float, default=0.1)
    p7.add_argument("--elephant-threshold-bytes", type=int, default=100_000)
    p7.add_argument("--csv", default="results/mice_elephants.csv")
    p7.add_argument("--plot-bars", default="figures/mice_elephants_slowdown_bars.png")
    p7.add_argument("--plot-p99-bars", default="figures/mice_elephants_mouse_p99_bars.png")
    p7.add_argument("--plot-mouse-line", default="figures/mice_elephants_mouse_p99_vs_load.png")
    p7.add_argument("--plot-elephant-line", default="figures/mice_elephants_elephant_goodput_vs_load.png")
    p7.add_argument("--plot-cdf", default="figures/mice_elephants_mouse_fct_cdf.png")
    p7.add_argument("--plot-pie", default="figures/mice_elephants_bytes_pie.png")
    p7.set_defaults(func=cmd_mice_elephants)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
