#!/usr/bin/env python3
"""Draw a fat-tree topology diagram (core/agg/edge/host layers, pods grouped),
optionally with failures applied and marked in red.

Usage:
    python visualize.py --k 8 --link-pct 5 --seed 1 --out figures/topology_k8.png
    python visualize.py --k 4 --no-failures
"""
import argparse
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

from fattree.config import load_config, validate_config
from fattree.failures import apply_failures
from fattree.topology import build_topology, validate_topology

LAYER_COLORS = {"core": "#2a6f97", "agg": "#52b69a", "edge": "#f4a259", "host": "#8d99ae"}
LAYER_Y = {"host": 0, "edge": 1, "agg": 2, "core": 3}


def layout(g, k):
    """Layer nodes by type (core top, host bottom), grouped by pod left to right."""
    half = k // 2
    pod_width = half * half
    pod_gap = max(1, half // 2)
    pos = {}

    for n, d in g.nodes(data=True):
        if d["type"] == "host":
            pod, e, host_id = d["pod"], d["switch"], d["host_id"]
            x = pod * (pod_width + pod_gap) + e * half + (host_id - 2)
            pos[n] = (x, LAYER_Y["host"])
        elif d["type"] == "edge":
            pod, s = d["pod"], d["switch"]
            x = pod * (pod_width + pod_gap) + s * half + (half - 1) / 2
            pos[n] = (x, LAYER_Y["edge"])
        elif d["type"] == "agg":
            pod, s = d["pod"], d["switch"]
            a_local = s - half
            x = pod * (pod_width + pod_gap) + (a_local + 0.5) * half
            pos[n] = (x, LAYER_Y["agg"])

    total_width = k * (pod_width + pod_gap) - pod_gap
    cores = sorted((n for n, d in g.nodes(data=True) if d["type"] == "core"),
                   key=lambda n: (g.nodes[n]["j"], g.nodes[n]["i"]))
    for idx, n in enumerate(cores):
        x = (idx + 0.5) * total_width / len(cores)
        pos[n] = (x, LAYER_Y["core"])

    return pos


def draw_fat_tree(g, k, out_path, title=None, failed_links=None, failed_switches=None):
    failed_links = failed_links or set()
    failed_switches = failed_switches or set()
    pos = layout(g, k)
    max_x = max(x for x, _ in pos.values())

    fig_width = min(30, max(8, max_x / 8))
    fig, ax = plt.subplots(figsize=(fig_width, 6))

    for u, v, d in g.edges(data=True):
        x1, y1 = pos[u]
        x2, y2 = pos[v]
        failed = (u, v) in failed_links or (v, u) in failed_links
        ax.plot([x1, x2], [y1, y2],
                color="#d00000" if failed else "#c9c9c9",
                linestyle="--" if failed else "-",
                linewidth=1.3 if failed else 0.6,
                alpha=0.95 if failed else 0.6,
                zorder=1)

    for n, d in g.nodes(data=True):
        x, y = pos[n]
        failed = n in failed_switches
        ax.scatter(x, y,
                   s=100 if d["type"] != "host" else 16,
                   c="#d00000" if failed else LAYER_COLORS[d["type"]],
                   marker="x" if failed else ("s" if d["type"] != "host" else "o"),
                   zorder=2, linewidths=1.8 if failed else 0)

    ax.set_xticks([])
    ax.set_yticks(list(LAYER_Y.values()))
    ax.set_yticklabels(list(LAYER_Y.keys()))
    ax.set_title(title or f"Topology, k={k}")
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)

    legend_elems = [
        Line2D([0], [0], marker="s", color="w", markerfacecolor=LAYER_COLORS["core"], markersize=9, label="core"),
        Line2D([0], [0], marker="s", color="w", markerfacecolor=LAYER_COLORS["agg"], markersize=9, label="agg"),
        Line2D([0], [0], marker="s", color="w", markerfacecolor=LAYER_COLORS["edge"], markersize=9, label="edge"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor=LAYER_COLORS["host"], markersize=7, label="host"),
    ]
    if failed_links or failed_switches:
        legend_elems.append(Line2D([0], [0], color="#d00000", linestyle="--", label="failed link"))
        legend_elems.append(Line2D([0], [0], marker="x", color="#d00000", linestyle="none",
                                    markersize=8, label="failed switch"))
    ax.legend(handles=legend_elems, loc="upper center", bbox_to_anchor=(0.5, -0.05),
              ncol=len(legend_elems), frameon=False, fontsize=8)

    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def draw_summary(g, k, failed, failures_mode, out_path):
    """Small 2-panel summary: pie of link status, bar of switch/link counts before vs after."""
    total_links = g.number_of_edges()
    failed_links_n = len(failed["failed_links"])
    alive_links_n = total_links - failed_links_n

    total_switches = sum(1 for _, d in g.nodes(data=True) if d["type"] != "host")
    failed_switches_n = len(failed["failed_switches"])
    alive_switches_n = total_switches - failed_switches_n

    fig, (ax_pie, ax_bar) = plt.subplots(1, 2, figsize=(9, 4))

    if failed_links_n:
        ax_pie.pie([alive_links_n, failed_links_n], labels=["alive", "failed"],
                   colors=["#52b69a", "#d00000"], autopct="%1.0f%%", startangle=90)
    else:
        ax_pie.pie([1], labels=["alive (0 failures)"], colors=["#52b69a"])
    ax_pie.set_title(f"Link status ({total_links} total)")

    categories = ["switches", "links"]
    before = [total_switches, total_links]
    after = [alive_switches_n, alive_links_n]
    x = range(len(categories))
    width = 0.35
    ax_bar.bar([i - width / 2 for i in x], before, width, label="before failures", color="#8d99ae")
    ax_bar.bar([i + width / 2 for i in x], after, width, label="after failures", color="#2a6f97")
    ax_bar.set_xticks(list(x))
    ax_bar.set_xticklabels(categories)
    ax_bar.set_title("Counts before vs after")
    ax_bar.legend(frameon=False, fontsize=8)
    for spine in ("top", "right"):
        ax_bar.spines[spine].set_visible(False)

    fig.suptitle(f"k={k} — link_pct={failed['link_pct']:.1f}%, switch_pct={failed['switch_pct']:.1f}% "
                 f"({failures_mode})")
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_switches_multifail(rows, p_pct, out_path):
    """Line plot: switches with >=2 failed links vs k -- simulation (mean +/- 95%
    CI) vs. the closed-form theory curve (assignment Sec. 5 "intermediate" example:
    switches with multiple failed links at a fixed %% vs. switch port count)."""
    ks = [r["k"] for r in rows]
    means = [r["mean"] for r in rows]
    cis = [r["ci95"] for r in rows]
    theory = [r["theory"] for r in rows]

    fig, ax = plt.subplots(figsize=(7, 5))
    ax.errorbar(ks, means, yerr=cis, fmt="o-", color="#2a6f97", capsize=3,
                label=f"simulation (mean ± 95% CI, {rows[0]['trials']} trials)")
    ax.plot(ks, theory, "--", color="#d00000",
            label=r"theory: $\left(1-(1-p)^k-kp(1-p)^{k-1}\right)\cdot\mathrm{switches}$")
    ax.set_xlabel("switch port count k")
    ax.set_ylabel(f"switches with ≥2 failed links (p={p_pct}%)")
    ax.set_title(f"Switches with multiple failed links vs. k (p={p_pct}% per link)")
    ax.legend(frameon=False, fontsize=8)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def parse_args():
    p = argparse.ArgumentParser(description="Draw a fat-tree topology diagram")
    p.add_argument("-c", "--config", default=None, help="path to YAML config")
    p.add_argument("--k", type=int, help="switch port count (overrides config)")
    p.add_argument("--topology-type", choices=["fat-tree", "traditional-tree"],
                    help="topology to draw (overrides config)")
    p.add_argument("--link-pct", type=float, help="link failure percentage")
    p.add_argument("--switch-pct", type=float, help="switch failure percentage")
    p.add_argument("--link-pct-range", type=float, nargs=2, metavar=("MIN", "MAX"),
                    help="randomize link failure pct uniformly in [MIN, MAX] (seeded)")
    p.add_argument("--switch-pct-range", type=float, nargs=2, metavar=("MIN", "MAX"),
                    help="randomize switch failure pct uniformly in [MIN, MAX] (seeded)")
    p.add_argument("--mode", choices=["exact", "bernoulli"], help="failure selection mode")
    p.add_argument("--seed", type=int, help="random seed")
    p.add_argument("--no-failures", action="store_true", help="draw the topology with no failures applied")
    p.add_argument("--no-summary", action="store_true", help="skip the pie/bar summary chart")
    p.add_argument("--out", default=None, help="topology diagram PNG path (default figures/topology_k<k>.png)")
    p.add_argument("--summary-out", default=None, help="summary chart PNG path (default figures/summary_k<k>.png)")
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
    if args.seed is not None:
        cfg["seed"] = args.seed
    return cfg


def main():
    args = parse_args()
    cfg = apply_overrides(load_config(args.config), args)
    validate_config(cfg)

    k = cfg["topology"]["k"]
    topology_type = cfg["topology"]["type"]
    g = build_topology(k, topology_type)
    validate_topology(g, k, topology_type)

    failed_links, failed_switches, failed = None, None, None
    title = f"{topology_type}, k={k}"
    if not args.no_failures:
        _, failed = apply_failures(g, cfg["failures"], cfg["seed"])
        failed_links, failed_switches = failed["failed_links"], failed["failed_switches"]
        title += (f" — {len(failed_switches)} switch / {len(failed_links)} link failures "
                  f"(link_pct={failed['link_pct']:.1f}%, switch_pct={failed['switch_pct']:.1f}%, "
                  f"{cfg['failures']['mode']})")

    out_path = args.out or f"figures/topology_{topology_type}_k{k}.png"
    path = draw_fat_tree(g, k, out_path, title=title,
                          failed_links=failed_links, failed_switches=failed_switches)
    print(f"wrote {path}")

    if failed is not None and not args.no_summary:
        summary_out = args.summary_out or f"figures/summary_{topology_type}_k{k}.png"
        path = draw_summary(g, k, failed, cfg["failures"]["mode"], summary_out)
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
