#!/usr/bin/env python3
"""CLI entry point: build a fat-tree, apply failures, print a summary.

Usage:
    python run.py -c config/default.yaml --k 16 --link-pct 10
"""
import argparse
import json
import sys

from fattree.config import load_config, validate_config
from fattree.failures import apply_failures
from fattree.topology import build_topology, validate_topology


def parse_args():
    p = argparse.ArgumentParser(description="Fat-tree topology simulator")
    p.add_argument("-c", "--config", default=None, help="path to YAML config")
    p.add_argument("--k", type=int, help="switch port count (overrides config)")
    p.add_argument("--topology-type", choices=["fat-tree", "traditional-tree"],
                    help="topology to build (overrides config)")
    p.add_argument("--link-pct", type=float, help="link failure percentage")
    p.add_argument("--switch-pct", type=float, help="switch failure percentage")
    p.add_argument("--link-pct-range", type=float, nargs=2, metavar=("MIN", "MAX"),
                    help="randomize link failure pct uniformly in [MIN, MAX] (seeded)")
    p.add_argument("--switch-pct-range", type=float, nargs=2, metavar=("MIN", "MAX"),
                    help="randomize switch failure pct uniformly in [MIN, MAX] (seeded)")
    p.add_argument("--mode", choices=["exact", "bernoulli"], help="failure selection mode")
    p.add_argument("--pattern", help="traffic pattern")
    p.add_argument("--load", type=float, help="offered load per host")
    p.add_argument("--routing", choices=["ecmp", "two-level", "single-path"], help="routing scheme")
    p.add_argument("--seed", type=int, help="random seed")
    p.add_argument("--trials", type=int, help="number of trials")
    p.add_argument("--json", action="store_true", help="print summary as JSON")
    return p.parse_args()


def apply_overrides(cfg, args):
    if args.k is not None:
        cfg["topology"]["k"] = args.k
    if args.topology_type is not None:
        cfg["topology"]["type"] = args.topology_type
    if args.link_pct is not None:
        cfg["failures"]["link_pct"] = args.link_pct
    if args.switch_pct is not None:
        cfg["failures"]["switch_pct"] = args.switch_pct
    if args.link_pct_range is not None:
        cfg["failures"]["link_pct"] = list(args.link_pct_range)
    if args.switch_pct_range is not None:
        cfg["failures"]["switch_pct"] = list(args.switch_pct_range)
    if args.mode is not None:
        cfg["failures"]["mode"] = args.mode
    if args.pattern is not None:
        cfg["traffic"]["pattern"] = args.pattern
    if args.load is not None:
        cfg["traffic"]["load"] = args.load
    if args.routing is not None:
        cfg["routing"] = args.routing
    if args.seed is not None:
        cfg["seed"] = args.seed
    if args.trials is not None:
        cfg["trials"] = args.trials
    return cfg


def summarize(g, g_failed, failed, k, topology_type):
    hosts = sum(1 for _, d in g.nodes(data=True) if d["type"] == "host")
    return {
        "k": k,
        "topology_type": topology_type,
        "hosts": hosts,
        "switches": g.number_of_nodes() - hosts,
        "links": g.number_of_edges(),
        "link_pct": round(failed["link_pct"], 2),
        "switch_pct": round(failed["switch_pct"], 2),
        "failed_switches": len(failed["failed_switches"]),
        "failed_links": len(failed["failed_links"]),
        "switches_after_failure": g_failed.number_of_nodes() - hosts,
        "links_after_failure": g_failed.number_of_edges(),
    }


def main():
    args = parse_args()
    try:
        cfg = apply_overrides(load_config(args.config), args)
        validate_config(cfg)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    k = cfg["topology"]["k"]
    topology_type = cfg["topology"]["type"]
    g = build_topology(k, topology_type)
    validate_topology(g, k, topology_type)

    g_failed, failed = apply_failures(g, cfg["failures"], cfg["seed"])
    summary = summarize(g, g_failed, failed, k, topology_type)

    if args.json:
        print(json.dumps(summary, indent=2))
        return

    print(f"Topology: {topology_type}, k={k}")
    print(f"  hosts:    {summary['hosts']}")
    print(f"  switches: {summary['switches']}")
    print(f"  links:    {summary['links']}")
    print(f"  failures: {summary['failed_switches']} switches, {summary['failed_links']} links "
          f"(link_pct={summary['link_pct']}%, switch_pct={summary['switch_pct']}%, "
          f"{cfg['failures']['mode']} mode)")
    print(f"  after failure: {summary['switches_after_failure']} switches, "
          f"{summary['links_after_failure']} links")


if __name__ == "__main__":
    main()
