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
from matplotlib.patches import Patch

from fattree.config import load_config, validate_config
from fattree.failures import apply_failures
from fattree.topology import build_topology, validate_topology

LAYER_COLORS = {"core": "#2a6f97", "agg": "#52b69a", "edge": "#f4a259", "host": "#8d99ae"}
LAYER_Y = {"host": 0, "edge": 1, "agg": 2, "core": 3}

plt.rcParams["font.size"] = 11  # Phase 4 graph rule: readable, >= 11pt

# Consistent colors across every Phase 4 (E1/E2/E3) figure: one color per k,
# and one per routing scheme/buffer size/topology (PHASE4.md Step 3 rule 5).
K_COLORS = {4: "#8d99ae", 8: "#2a6f97", 16: "#52b69a", 24: "#f4a259", 32: "#d00000", 48: "#7b2cbf", 64: "#1b4332"}
SCHEME_COLORS = {
    "single-path": "#8d99ae", "ecmp": "#f4a259", "two-level": "#2a6f97",
    "two-level-failover": "#52b69a", "classifier": "#52b69a", "scheduler": "#d00000",
}
TOPOLOGY_COLORS = {"fat-tree": "#2a6f97", "traditional-tree": "#d00000"}


def _no_top_right_spines(ax):
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)


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


def plot_critical_failure_pct(rows, out_path):
    """Line plot: smallest link failure %% (bernoulli) that drops mean host-pair
    connectivity below 99.9%% / 99%%, vs. switch port count k."""
    ks = [r["k"] for r in rows]
    p999 = [r["critical_pct_99_9"] for r in rows]
    p99 = [r["critical_pct_99"] for r in rows]

    fig, ax = plt.subplots(figsize=(7, 5))
    ax.plot(ks, p999, "o-", color="#2a6f97", label="connectivity drops below 99.9%")
    ax.plot(ks, p99, "s-", color="#52b69a", label="connectivity drops below 99%")
    ax.set_xlabel("switch port count k")
    ax.set_ylabel("critical link failure %")
    ax.set_title("Critical link failure % vs. k (bernoulli, edge-agg + agg-core)")
    ax.legend(frameon=False, fontsize=9)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_table2(rows, patterns, paper_table2, out_path):
    """Grouped bar chart: throughput %% of ideal, one group per pattern, one bar
    per scheme, with the paper's own Table 2 values overlaid as markers."""
    schemes = ["tree", "two-level", "classifier", "scheduler"]
    colors = {"tree": "#8d99ae", "two-level": "#2a6f97", "classifier": "#52b69a", "scheduler": "#d00000"}
    pattern_names = [name for name, _ in patterns]
    n_patterns = len(pattern_names)
    n_schemes = len(schemes)
    width = 0.8 / n_schemes

    by_scheme = {s: {r["pattern"]: r for r in rows if r["scheme"] == s} for s in schemes}

    fig, ax = plt.subplots(figsize=(13, 6))
    x = range(n_patterns)
    for i, scheme in enumerate(schemes):
        means = [by_scheme[scheme][p]["mean_pct"] for p in pattern_names]
        cis = [by_scheme[scheme][p]["ci95"] for p in pattern_names]
        offsets = [xi + (i - (n_schemes - 1) / 2) * width for xi in x]
        ax.bar(offsets, means, width, yerr=cis, capsize=2, color=colors[scheme], label=scheme)
        paper_vals = paper_table2[scheme]
        ax.scatter(offsets, paper_vals, marker="_", color="black", s=200, linewidths=1.5,
                   label="paper value" if i == 0 else None, zorder=3)

    ax.set_xticks(list(x))
    ax.set_xticklabels(pattern_names, rotation=30, ha="right")
    ax.set_ylabel("throughput (% of ideal)")
    ax.set_ylim(0, 110)
    ax.set_title("Table 2 reproduction: throughput vs. traffic pattern, by routing scheme\n"
                 "(bars = our flow-level simulation, black ticks = paper's reported values)")
    ax.legend(frameon=False, fontsize=8, ncol=5, loc="upper center", bbox_to_anchor=(0.5, -0.25))
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_throughput_vs_failures(rows, ks, schemes, out_path):
    """Line plots with 95% CI bands: throughput %% of ideal vs. link failure %%,
    one curve per routing scheme, one subplot per k."""
    colors = {
        "two-level": "#2a6f97", "two-level-failover": "#52b69a",
        "ecmp": "#f4a259", "classifier": "#8d99ae", "scheduler": "#d00000",
    }
    fig, axes = plt.subplots(1, len(ks), figsize=(6 * len(ks), 5), squeeze=False)
    axes = axes[0]

    for ax, k in zip(axes, ks):
        k_rows = [r for r in rows if r["k"] == k]
        for scheme in schemes:
            scheme_rows = sorted((r for r in k_rows if r["scheme"] == scheme), key=lambda r: r["link_pct"])
            if not scheme_rows:
                continue
            pcts = [r["link_pct"] for r in scheme_rows]
            means = [r["mean_pct"] for r in scheme_rows]
            cis = [r["ci95"] for r in scheme_rows]
            ax.errorbar(pcts, means, yerr=cis, fmt="o-", capsize=2, color=colors.get(scheme), label=scheme)
        ax.set_xlabel("link failure %")
        ax.set_ylabel("throughput (% of ideal)")
        ax.set_title(f"k={k}")
        ax.set_ylim(0, 105)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
    axes[0].legend(frameon=False, fontsize=8)
    fig.suptitle("Throughput vs. link failure %, by routing scheme")
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_link_vs_switch_failures(rows, pcts, out_path):
    """Two line plots (connectivity, path diversity) vs. failure %%, one curve
    each for link failures and switch failures at the same %%."""
    colors = {"link": "#2a6f97", "switch": "#d00000"}
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    for failure_type in ("link", "switch"):
        type_rows = sorted((r for r in rows if r["failure_type"] == failure_type), key=lambda r: r["pct"])
        p = [r["pct"] for r in type_rows]
        fc = [r["fraction_connected_mean"] for r in type_rows]
        fc_ci = [r["fraction_connected_ci95"] for r in type_rows]
        pd = [r["path_diversity_mean"] for r in type_rows]
        pd_ci = [r["path_diversity_ci95"] for r in type_rows]
        ax1.errorbar(p, fc, yerr=fc_ci, fmt="o-", capsize=2, color=colors[failure_type],
                     label=f"{failure_type} failures")
        ax2.errorbar(p, pd, yerr=pd_ci, fmt="o-", capsize=2, color=colors[failure_type],
                     label=f"{failure_type} failures")

    ax1.set_xlabel("failure %")
    ax1.set_ylabel("fraction of host pairs connected")
    ax1.set_title("Connectivity")
    ax2.set_xlabel("failure %")
    ax2.set_ylabel("surviving paths / (k/2)²")
    ax2.set_title("Inter-pod path diversity (pods 0,1)")
    for ax in (ax1, ax2):
        ax.legend(frameon=False, fontsize=9)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
    fig.suptitle("Link failures vs. switch failures at equal %")
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_incast_goodput_vs_m(rows, group_key, group_label, out_path):
    """Line plot: incast goodput (% of receiver line rate) vs. number of
    senders M, one curve per distinct value of `group_key` (the "collapse"
    figure -- PHASE3_2_EXP9_INCAST.md figures 1-3)."""
    groups = sorted({row[group_key] for row in rows}, key=str)
    colors = plt.cm.viridis([i / max(1, len(groups) - 1) for i in range(len(groups))])

    fig, ax = plt.subplots(figsize=(7, 5))
    for color, group in zip(colors, groups):
        group_rows = sorted((r for r in rows if r[group_key] == group), key=lambda r: r["m"])
        ms = [r["m"] for r in group_rows]
        means = [r["goodput_pct_of_line_rate"] for r in group_rows]
        cis = [r["goodput_ci95_pct"] for r in group_rows]
        ax.errorbar(ms, means, yerr=cis, fmt="o-", capsize=3, color=color, label=f"{group_label}={group}")
    ax.set_xlabel("number of senders M")
    ax.set_ylabel("goodput (% of receiver line rate)")
    ax.set_ylim(0, 105)
    ax.set_title(f"Incast goodput vs. M, by {group_label}")
    ax.legend(frameon=False, fontsize=9)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_incast_drop_location(topo_rows, out_path):
    """Pie charts (side by side): where incast packets are dropped (by layer),
    fat-tree vs. traditional tree, at the largest M in `topo_rows`."""
    topologies = sorted({row["topology"] for row in topo_rows})
    fig, axes = plt.subplots(1, len(topologies), figsize=(6 * len(topologies), 5))
    if len(topologies) == 1:
        axes = [axes]
    colors = {"host": "#d00000", "edge": "#f4a259", "agg": "#52b69a", "core": "#2a6f97"}

    for ax, topo in zip(axes, topologies):
        topo_group = [r for r in topo_rows if r["topology"] == topo]
        row = max(topo_group, key=lambda r: r["m"])
        drops = row["_drops_by_layer"]
        labels = [k for k, v in drops.items() if v > 0]
        values = [v for v in drops.values() if v > 0]
        if values:
            ax.pie(values, labels=labels, colors=[colors.get(l, "#999") for l in labels],
                   autopct="%1.0f%%", startangle=90)
        else:
            ax.text(0.5, 0.5, "no drops", ha="center", va="center", transform=ax.transAxes)
        ax.set_title(f"{topo} (M={row['m']})")
    fig.suptitle("Where incast packets are dropped, by layer")
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_incast_timeouts_vs_m(rows, out_path):
    """Line plot: RTO timeouts per round vs. M, one curve per buffer size."""
    groups = sorted({row["queue_size_packets"] for row in rows})
    colors = plt.cm.viridis([i / max(1, len(groups) - 1) for i in range(len(groups))])

    fig, ax = plt.subplots(figsize=(7, 5))
    for color, q in zip(colors, groups):
        group_rows = sorted((r for r in rows if r["queue_size_packets"] == q), key=lambda r: r["m"])
        ax.plot([r["m"] for r in group_rows], [r["timeouts_per_round"] for r in group_rows],
                "o-", color=color, label=f"buffer={q}")
    ax.set_xlabel("number of senders M")
    ax.set_ylabel("RTO timeouts per round")
    ax.set_title("Incast timeouts vs. M, by buffer size")
    ax.legend(frameon=False, fontsize=9)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_incast_queue_series(samples, m, out_path):
    """Queue length at the receiver's edge port over time, for one round."""
    fig, ax = plt.subplots(figsize=(8, 4))
    times_ms = [t / 1e6 for t, _ in samples]
    lens = [n for _, n in samples]
    ax.step(times_ms, lens, where="post", color="#2a6f97")
    ax.set_xlabel("time (ms)")
    ax.set_ylabel("queue length (packets)")
    ax.set_title(f"Receiver edge-port queue length over one round, M={m}")
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_mice_elephants_bars(bar_data, out_path, ylabel="slowdown (FCT / ideal FCT)",
                              title="Mean slowdown by scheme, mice vs. elephants (load=0.5)", single=False):
    """Grouped bar chart: mean slowdown per scheme, mice vs. elephants (or a
    single series, e.g. mouse p99, when `single=True`)."""
    schemes = [s for s in SCHEME_COLORS if s in bar_data]
    x = range(len(schemes))
    fig, ax = plt.subplots(figsize=(8, 5))
    if single:
        values = [bar_data[s][0] or 0 for s in schemes]
        bars = ax.bar(list(x), values, color=[SCHEME_COLORS[s] for s in schemes])
        for bar, s in zip(bars, schemes):
            if bar_data[s][0] is None:
                ax.text(bar.get_x() + bar.get_width() / 2, 0.02, "N/A", ha="center", va="bottom", fontsize=8)
    else:
        width = 0.35
        mouse_vals = [bar_data[s][0] or 0 for s in schemes]
        eleph_vals = [bar_data[s][1] or 0 for s in schemes]
        ax.bar([i - width / 2 for i in x], mouse_vals, width, label="mice", color="#2a6f97")
        bars = ax.bar([i + width / 2 for i in x], eleph_vals, width, label="elephants", color="#d00000")
        for bar, s in zip(bars, schemes):
            if bar_data[s][1] is None:
                ax.text(bar.get_x() + bar.get_width() / 2, 0.02, "N/A", ha="center", va="bottom", fontsize=8)
        ax.legend(frameon=False, fontsize=9)
    ax.set_xticks(list(x))
    ax.set_xticklabels(schemes, rotation=20, ha="right")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_mice_elephants_line(rows, schemes, field, ylabel, out_path):
    """Line plot: `field` vs. load, one curve per routing scheme."""
    fig, ax = plt.subplots(figsize=(7, 5))
    for scheme in schemes:
        scheme_rows = sorted((r for r in rows if r["scheme"] == scheme and r[field] is not None),
                             key=lambda r: r["load"])
        if not scheme_rows:
            continue
        ax.plot([r["load"] for r in scheme_rows], [r[field] for r in scheme_rows],
                "o-", color=SCHEME_COLORS.get(scheme), label=scheme)
    ax.set_xlabel("offered load")
    ax.set_ylabel(ylabel)
    ax.set_title(f"{ylabel} vs. load, by scheme")
    ax.legend(frameon=False, fontsize=9)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_mice_elephants_mouse_fct_cdf(cdf_data, out_path):
    """CDF of mouse FCT, one curve per scheme (load=0.7)."""
    fig, ax = plt.subplots(figsize=(7, 5))
    for scheme, fcts_ns in cdf_data.items():
        if not fcts_ns:
            continue
        fcts_ms = [t / 1e6 for t in fcts_ns]
        y = [i / len(fcts_ms) for i in range(1, len(fcts_ms) + 1)]
        ax.plot(fcts_ms, y, color=SCHEME_COLORS.get(scheme), label=scheme)
    ax.set_xlabel("mouse FCT (ms)")
    ax.set_ylabel("CDF")
    ax.set_title("Mouse FCT CDF by scheme (load=0.7)")
    ax.legend(frameon=False, fontsize=9)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_mice_elephants_bytes_pie(flow_share, byte_share, out_path):
    """Pie charts: share of flows vs. share of bytes by class (shows "few
    elephants, most bytes")."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 5))
    colors = {"mouse": "#2a6f97", "elephant": "#d00000"}
    for ax, data, title in ((ax1, flow_share, "share of flows"), (ax2, byte_share, "share of bytes")):
        labels = list(data.keys())
        values = list(data.values())
        ax.pie(values, labels=labels, colors=[colors[l] for l in labels], autopct="%1.0f%%", startangle=90)
        ax.set_title(title)
    fig.suptitle("Mice vs. elephants: few flows, most bytes")
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


# --- Phase 4, E1: Resilience (PHASE4.md Steps 3-4) --------------------------------------------------------------------

def plot_e1_connectivity(rows, ks, out_path):
    """Fraction of server pairs connected vs. link failure %, one line per k,
    with the theory curve 1 - 2*p^(k/2) dashed. Annotates where each k's
    curve drops below 99.9%."""
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for k in ks:
        k_rows = sorted((r for r in rows if r["k"] == k), key=lambda r: r["link_pct"])
        pcts = [r["link_pct"] for r in k_rows]
        means = [100 * r["fraction_connected_mean"] for r in k_rows]
        cis = [100 * r["fraction_connected_ci95"] for r in k_rows]
        theory = [100 * r["theory_connected"] for r in k_rows]
        color = K_COLORS.get(k, "#333333")
        ax.errorbar(pcts, means, yerr=cis, fmt="o-", capsize=2, color=color, label=f"k={k} (sim)")
        ax.plot(pcts, theory, "--", color=color, alpha=0.6, label=f"k={k} theory: 1-2p^(k/2)")

        below = next((r["link_pct"] for r in k_rows if r["fraction_connected_mean"] < 0.999), None)
        if below is not None:
            ax.annotate(f"k={k}: <99.9% at {below:.0f}%", xy=(below, 99.9), xytext=(below + 3, 99.9 - 12),
                        fontsize=9, color=color, arrowprops=dict(arrowstyle="->", color=color, lw=1))
    ax.axhline(99.9, color="gray", linestyle=":", linewidth=0.8)
    ax.set_xlabel("link failure % (bernoulli, fabric links)")
    ax.set_ylabel("server pairs connected (%)")
    ax.set_ylim(0, 102)
    ax.set_title("Bigger fat-trees survive far more broken links")
    ax.legend(frameon=False, fontsize=9, ncol=2)
    _no_top_right_spines(ax)
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_e1_path_diversity(rows, ks, out_path):
    """Surviving inter-pod shortest paths / (k/2)^2 vs. link failure %, one
    line per k, with a single theory curve (1-p)^4, k-independent since it's
    the survival probability of one specific 4-fabric-link path."""
    fig, ax = plt.subplots(figsize=(8, 4.5))
    all_pcts = sorted({r["link_pct"] for r in rows})
    theory = [100 * (1 - p / 100) ** 4 for p in all_pcts]
    ax.plot(all_pcts, theory, "--", color="black", alpha=0.6, label="theory: (1-p)^4")
    for k in ks:
        k_rows = sorted((r for r in rows if r["k"] == k), key=lambda r: r["link_pct"])
        pcts = [r["link_pct"] for r in k_rows]
        means = [100 * r["path_diversity_mean"] for r in k_rows]
        cis = [100 * r["path_diversity_ci95"] for r in k_rows]
        ax.errorbar(pcts, means, yerr=cis, fmt="o-", capsize=2, color=K_COLORS.get(k, "#333"), label=f"k={k}")

    half_point = 100 * (1 - 0.5 ** (1 / 4))  # exact (1-p)^4 = 0.5 crossing, ~15.9%, not the nearest data point
    if all_pcts and all_pcts[0] <= half_point <= all_pcts[-1]:
        ax.annotate("half the paths are already gone here", xy=(half_point, 50),
                    xytext=(half_point + 8, 65), fontsize=9,
                    arrowprops=dict(arrowstyle="->", color="black", lw=1))
    ax.set_xlabel("link failure % (bernoulli, fabric links)")
    ax.set_ylabel("surviving inter-pod paths / (k/2)² (%)")
    ax.set_ylim(0, 102)
    ax.set_title("Redundancy erodes long before connectivity is lost")
    ax.legend(frameon=False, fontsize=9)
    _no_top_right_spines(ax)
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_e1_layer(rows, out_path):
    """Grouped bars: % of server pairs disconnected when 5%/10% of switches
    in ONE layer fail, by layer (edge/agg/core)."""
    layers = ["edge", "agg", "core"]
    pcts = sorted({r["switch_pct"] for r in rows})
    colors = {pcts[0]: "#2a6f97", pcts[-1]: "#d00000"} if len(pcts) > 1 else {pcts[0]: "#2a6f97"}

    by_layer = {layer: {r["switch_pct"]: r for r in rows if r["layer"] == layer} for layer in layers}
    x = range(len(layers))
    width = 0.8 / max(1, len(pcts))
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for i, pct in enumerate(pcts):
        means = [by_layer[layer][pct]["pct_disconnected_mean"] for layer in layers]
        cis = [by_layer[layer][pct]["pct_disconnected_ci95"] for layer in layers]
        offsets = [xi + (i - (len(pcts) - 1) / 2) * width for xi in x]
        bars = ax.bar(offsets, means, width, yerr=cis, capsize=3, color=colors.get(pct, "#333"),
                      label=f"{pct}% of switches")
        for bar, mean in zip(bars, means):
            if mean == 0:  # a zero bar is invisible -- label it so it reads as "measured zero", not "missing"
                ax.text(bar.get_x() + bar.get_width() / 2, 0.3, "0%", ha="center", va="bottom", fontsize=8)
    ax.set_xticks(list(x))
    ax.set_xticklabels(layers)
    ax.set_ylabel("server pairs disconnected (%)")
    ax.set_title("An edge failure cuts off its own servers; core/agg barely touch connectivity")
    ax.legend(frameon=False, fontsize=9)
    _no_top_right_spines(ax)
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


# --- Phase 4, E2: Throughput (PHASE4.md Steps 3, 5) --------------------------------------------------------------------

def plot_e2_vs_failures(rows, schemes, out_path):
    """Throughput (% of ideal) vs. link failure %, one line per scheme.
    Annotates the gap between two-level and ecmp at the highest failure %."""
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for scheme in schemes:
        s_rows = sorted((r for r in rows if r["scheme"] == scheme), key=lambda r: r["link_pct"])
        pcts = [r["link_pct"] for r in s_rows]
        means = [r["mean_pct"] for r in s_rows]
        cis = [r["ci95"] for r in s_rows]
        ax.errorbar(pcts, means, yerr=cis, fmt="o-", capsize=3, color=SCHEME_COLORS.get(scheme), label=scheme)

    by_scheme = {s: {r["link_pct"]: r["mean_pct"] for r in rows if r["scheme"] == s} for s in schemes}
    max_pct = max(r["link_pct"] for r in rows)
    if "two-level" in by_scheme and "ecmp" in by_scheme and max_pct in by_scheme["two-level"]:
        y_lo = by_scheme["two-level"][max_pct]
        y_hi = by_scheme["ecmp"][max_pct]
        ax.annotate("", xy=(max_pct, y_hi), xytext=(max_pct, y_lo),
                    arrowprops=dict(arrowstyle="<->", color="black", lw=1))
        ax.text(max_pct - 2, (y_lo + y_hi) / 2, f"{y_hi - y_lo:.0f} pt gap", fontsize=9, ha="right")

    ax.set_xlabel("link failure % (bernoulli, fabric links)")
    ax.set_ylabel("throughput (% of ideal)")
    ax.set_ylim(0, 105)
    ax.set_title("Static routing tables lose the most as links fail")
    ax.legend(frameon=False, fontsize=9)
    _no_top_right_spines(ax)
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_e2_collision_ceiling(rows, schemes, out_path):
    """Throughput (% of ideal) at 0% failures vs. k, one line per scheme, a
    100% reference line, and the analytic single-hop upper bound
    1 - (1-2/k)^(k/2) dashed. `two-level-failover` is dropped from this
    plot (not the CSV) -- at 0% failures it is numerically identical to
    `two-level` (failover only changes behavior once something is broken),
    so its line exactly overplots and hides the `two-level` line
    (PHASE5.md Step 2)."""
    schemes = [s for s in schemes if s != "two-level-failover"]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ks = sorted({r["k"] for r in rows})
    theory = [next(r["theory_pct"] for r in rows if r["k"] == k) for k in ks]
    ax.plot(ks, theory, "--", color="black", alpha=0.7, label="single-hop upper bound: 1-(1-2/k)^(k/2)")
    ax.axhline(100, color="gray", linestyle=":", linewidth=1)
    for scheme in schemes:
        s_rows = sorted((r for r in rows if r["scheme"] == scheme), key=lambda r: r["k"])
        means = [r["mean_pct"] for r in s_rows]
        cis = [r["ci95"] for r in s_rows]
        ax.errorbar(ks, means, yerr=cis, fmt="o-", capsize=3, color=SCHEME_COLORS.get(scheme), label=scheme)

    ax.annotate("full bisection bandwidth promises 100%", xy=(ks[-1], 100), xytext=(ks[-1] - 6, 88),
                fontsize=9, arrowprops=dict(arrowstyle="->", color="gray", lw=1))
    ax.set_xlabel("switch port count k")
    ax.set_ylabel("throughput at 0% failures (% of ideal)")
    ax.set_ylim(0, 110)
    ax.set_title("Hash collisions waste about half the capacity, even with no failures")
    ax.legend(frameon=False, fontsize=9)
    _no_top_right_spines(ax)
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


# --- Phase 4, E3: Incast (PHASE4.md Steps 3, 6) --------------------------------------------------------------------

def plot_e3_collapse(rows, ms, out_path, init_cwnd=10):
    """Goodput (% of receiver line rate) vs. number of senders M, one line
    per buffer size, min RTO 200ms. Annotates each buffer's collapse point
    and the analytic estimate M* ~= buffer / (2*init_cwnd)."""
    queue_sizes = sorted({r["queue_size_packets"] for r in rows})
    colors = plt.cm.viridis([i / max(1, len(queue_sizes) - 1) for i in range(len(queue_sizes))])

    fig, ax = plt.subplots(figsize=(8, 4.5))
    for color, q in zip(colors, queue_sizes):
        q_rows = sorted((r for r in rows if r["queue_size_packets"] == q), key=lambda r: r["m"])
        xs = [r["m"] for r in q_rows]
        ys = [r["goodput_pct"] for r in q_rows]
        cis = [r["goodput_ci95_pct"] for r in q_rows]
        ax.errorbar(xs, ys, yerr=cis, fmt="o-", capsize=3, color=color, label=f"buffer={q}")

        m_star = q / (2 * init_cwnd)
        ax.axvline(m_star, color=color, linestyle=":", linewidth=1, alpha=0.6)
        collapse_m = next((r["m"] for r in q_rows if r["goodput_pct"] < 50), None)
        if collapse_m is not None:
            ax.annotate(f"buffer={q} collapses\nat M={collapse_m}", xy=(collapse_m, 50),
                        xytext=(collapse_m + 1.5, 60 + 8 * queue_sizes.index(q)),
                        fontsize=8, color=color, arrowprops=dict(arrowstyle="->", color=color, lw=1))

    ax.set_xlabel("number of senders M")
    ax.set_ylabel("goodput (% of receiver line rate)")
    ax.set_ylim(0, 105)
    ax.set_title("A bigger buffer only delays the incast collapse")
    ax.legend(frameon=False, fontsize=9)
    _no_top_right_spines(ax)
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_e3_where_time_goes(rows, ms, out_path):
    """100%-stacked bars per M: share of the round spent sending data
    (bottom, blue, = goodput) vs. waiting for a timeout (top; red for the
    200ms floor, green for 1ms), one pair of bars (200ms | 1ms) per M.

    Replaces an earlier design (PHASE6.md Step 2) where bar *height* was
    round time (tall = slow) but the label on it was goodput (high = good)
    -- readers saw "2%" sitting high on a tall bar and "72%" low on a short
    bar and read it backwards. Every bar here is the same 100%-tall shape;
    only the split changes, so tall-blue always means "mostly useful."
    """
    rtos = [(200, "#D0021B", "200 ms timeout"), (1, "#4DB89A", "1 ms timeout")]
    send_color = "#2C6E99"
    width = 0.36

    fig, ax = plt.subplots(figsize=(8, 4.5))
    x = list(range(len(ms)))

    for i, (rto, wait_color, rto_label) in enumerate(rtos):
        rto_rows = {r["m"]: r for r in rows if r["min_rto_ms"] == rto}
        offsets = [xi + (i - 0.5) * width for xi in x]
        for xi, m in zip(offsets, ms):
            r = rto_rows[m]
            s, w = r["sending_ms"], r["waiting_ms"]
            total = s + w
            goodput_pct = 100 * s / total if total else 0.0
            ax.bar(xi, goodput_pct, width, color=send_color)
            ax.bar(xi, 100 - goodput_pct, width, bottom=goodput_pct, color=wait_color)
            total_label = f"{total:.1f} ms" if total < 10 else f"{total:.0f} ms"
            ax.text(xi, 102, total_label, ha="center", fontsize=9)
            ax.text(xi, max(goodput_pct / 2, 4), f"{goodput_pct:.0f}%", ha="center", va="center",
                    color="white", fontsize=10, fontweight="bold")

    ax.set_xticks(x)
    ax.set_xticklabels([f"M={m}\n200ms | 1ms" for m in ms])
    ax.set_ylabel("share of round time (%)")
    ax.set_ylim(0, 112)
    ax.set_title("With a 200ms timeout, almost the whole round is spent waiting")
    legend_handles = [
        Patch(color=rtos[0][1], label=f"waiting ({rtos[0][2]})"),
        Patch(color=rtos[1][1], label=f"waiting ({rtos[1][2]})"),
        Patch(color=send_color, label="sending data (useful)"),
    ]
    ax.legend(handles=legend_handles, frameon=False, fontsize=9, ncol=3,
              loc="upper center", bbox_to_anchor=(0.5, -0.15))
    _no_top_right_spines(ax)
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_e3_queue(queue_traces, buffer, topo_goodput, compare_m, out_path):
    """Queue length at the receiver's edge port over one round, for two M
    values, with the buffer limit as a horizontal line and drops marked
    with x. Adds a text box comparing fat-tree vs. traditional-tree
    goodput at `compare_m` (the bottleneck is the receiver's last cable,
    which neither topology's core richness changes)."""
    colors = {}
    ms = sorted(queue_traces)
    palette = ["#2a6f97", "#d00000", "#52b69a", "#f4a259"]
    for i, m in enumerate(ms):
        colors[m] = palette[i % len(palette)]

    fig, ax = plt.subplots(figsize=(8, 4.5))
    drain_times_ms = {}
    for m in ms:
        series, drop_times_ns = queue_traces[m]
        if not series:
            continue
        times_ms = [t / 1e6 for t, _ in series]
        lens = [n for _, n in series]
        ax.step(times_ms, lens, where="post", color=colors[m], label=f"M={m}")
        if drop_times_ns:
            ax.scatter([t / 1e6 for t in drop_times_ns], [buffer] * len(drop_times_ns),
                       marker="x", color=colors[m], s=40, zorder=3)
        # the burst fills and drains in the first ms or two; the rest of the
        # round (often 100s of ms of RTO wait, see e3_where_time_goes) is a
        # flat empty queue that would otherwise swamp the interesting part
        peak_idx = max(range(len(lens)), key=lambda i: lens[i])
        drain_idx = next((i for i in range(peak_idx, len(lens)) if lens[i] == 0), len(lens) - 1)
        drain_times_ms[m] = times_ms[drain_idx]

    ax.axhline(buffer, color="gray", linestyle="--", linewidth=1, label=f"buffer limit ({buffer} packets)")
    if drain_times_ms:
        x_max = max(drain_times_ms.values()) * 1.3
        ax.set_xlim(0, x_max)
        worst_m = max(drain_times_ms)  # the M with the longest fill/drain cycle -- the one that actually collapses
        drain_t = drain_times_ms[worst_m]
        ax.annotate("then idle ~200ms waiting for\nthe timeout (see previous figure)",
                    xy=(drain_t, 2), xytext=(min(drain_t + x_max * 0.08, x_max * 0.98), buffer * 0.3),
                    fontsize=8, color=colors[worst_m],
                    arrowprops=dict(arrowstyle="->", color=colors[worst_m], lw=1))
    ax.set_xlabel("time (ms)")
    ax.set_ylabel("queue length (packets)")
    ax.set_title("You can see the buffer fill and overflow")
    ax.legend(frameon=False, fontsize=9)
    _no_top_right_spines(ax)

    if topo_goodput:
        lines = [f"{name}: {pct:.1f}%" for name, pct in topo_goodput.items()]
        text = f"goodput at M={compare_m}:\n" + "\n".join(lines) + "\n(bottleneck: receiver's last cable)"
        ax.text(0.98, 0.95, text, transform=ax.transAxes, ha="right", va="top", fontsize=8,
                bbox=dict(boxstyle="round", facecolor="white", edgecolor="gray", alpha=0.9))

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
