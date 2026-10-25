"""Graph-level simulation: structure only, no traffic/routing (PHASE2_PROMPT.md
Step 4). Path metrics use the edge-switch aggregation trick: every host behind
the same edge switch is topologically interchangeable, so BFS runs on the (far
smaller) switch-only graph from each surviving edge switch, and host-pair metrics
are derived by weighting switch-pair distances by the number of surviving hosts
behind each switch -- O(edge_switches^2) instead of O(hosts^2), so k=48 finishes
in seconds rather than minutes.
"""
import networkx as nx
import numpy as np
import scipy.sparse as sp
from scipy.sparse.csgraph import shortest_path as _sp_shortest_path

from fattree.topology import core_switch_name, pod_switch_name


def _switch_subgraph(g):
    """A materialized (not a view) switch-only graph -- views are noticeably
    slower under repeated BFS, and this runs once per metrics call."""
    return nx.Graph(g.subgraph([n for n, d in g.nodes(data=True) if d["type"] != "host"]))


def _switch_pair_distances(g, switches):
    """Pairwise distances among `switches` (e.g. surviving edge switches), via
    scipy's sparse (C-optimized) BFS from each. Returns a plain (n, n) numpy
    array indexed by position in `switches` -- NOT a dict-of-dicts, since
    materializing len(switches) x total_switches Python dict entries (not the
    BFS itself) was the actual bottleneck at k=48+ scale (47s -> this is <1s)."""
    if not switches:
        return np.zeros((0, 0))
    nodes = list(g.nodes())
    index = {n: i for i, n in enumerate(nodes)}
    n = len(nodes)

    rows, cols = [], []
    for u, v in g.edges():
        rows.append(index[u]); cols.append(index[v])
        rows.append(index[v]); cols.append(index[u])
    adj = sp.csr_matrix((np.ones(len(rows)), (rows, cols)), shape=(n, n))

    idx = [index[s] for s in switches]
    full = _sp_shortest_path(adj, method="D", unweighted=True, directed=False, indices=idx)
    return full[:, idx]  # (len(switches), len(switches))


def _surviving_hosts_per_edge_switch(g_before, g_after):
    """{edge_switch_name: surviving_host_count}: a host counts only if both its
    edge switch and its specific host-edge link survived into g_after."""
    counts = {}
    for n, d in g_before.nodes(data=True):
        if d["type"] != "host":
            continue
        edge_name = next((nb for nb in g_before.neighbors(n) if g_before.nodes[nb]["type"] == "edge"), None)
        if edge_name is not None and g_after.has_node(edge_name) and g_after.has_edge(n, edge_name):
            counts[edge_name] = counts.get(edge_name, 0) + 1
    return counts


def connectivity_and_path_metrics(g_before, g_after):
    """Returns fraction_connected, isolated_hosts, avg_path_length, avg_stretch,
    diameter -- all via edge-switch BFS aggregation (CLAUDE.md Sec. 6).

    `avg_stretch` = actual_hops - ideal_hops (pre-failure shortest length for that
    same switch pair), averaged over pairs connected after failure.
    Hop counts include both host-edge hops (switch_dist + 2).
    """
    total_hosts = sum(1 for _, d in g_before.nodes(data=True) if d["type"] == "host")
    hosts_per_switch = _surviving_hosts_per_edge_switch(g_before, g_after)
    isolated = total_hosts - sum(hosts_per_switch.values())

    switch_sub_after = _switch_subgraph(g_after)
    switches = [s for s in hosts_per_switch if s in switch_sub_after]

    total_pairs = total_hosts * (total_hosts - 1) if total_hosts > 1 else 0

    if not switches:
        return {
            "fraction_connected": 0.0 if total_pairs else 1.0,
            "isolated_hosts": isolated,
            "avg_path_length": None,
            "avg_stretch": None,
            "diameter": None,
        }

    dist = _switch_pair_distances(switch_sub_after, switches)

    # Vectorized aggregation (numpy, not a Python double loop): looping switch
    # pairs one at a time in Python was the actual bottleneck at k=48+ scale, not
    # the BFS itself (1152^2 iterations ~= 2.5s vs <1s for everything else).
    n_hosts = np.array([hosts_per_switch[s] for s in switches], dtype=np.int64)
    pods = np.array([g_before.nodes[s]["pod"] for s in switches], dtype=np.int64)

    pairs = np.outer(n_hosts, n_hosts)
    np.fill_diagonal(pairs, n_hosts * (n_hosts - 1))

    hops = dist + 2  # diagonal: self-distance 0 + 2 = 2, correct
    ideal_switch_dist = np.where(pods[:, None] == pods[None, :], 2, 4)
    np.fill_diagonal(ideal_switch_dist, 0)  # same switch: ideal switch-distance is 0, not 2
    ideal = ideal_switch_dist + 2  # total host-level ideal, consistent with hops = dist + 2

    valid = np.isfinite(dist) & (pairs > 0)
    connected_pairs = int(pairs[valid].sum())
    weighted_hops = int((pairs[valid] * hops[valid]).sum())
    weighted_ideal_hops = int((pairs[valid] * ideal[valid]).sum())
    max_hops = int(hops[valid].max()) if valid.any() else 0

    return {
        "fraction_connected": connected_pairs / total_pairs if total_pairs else 1.0,
        "isolated_hosts": isolated,
        "avg_path_length": weighted_hops / connected_pairs if connected_pairs else None,
        "avg_stretch": (weighted_hops - weighted_ideal_hops) / connected_pairs if connected_pairs else None,
        "diameter": max_hops if connected_pairs else None,
    }


def _canonical_survivor_fraction(g_after, k, pod_a, pod_b, edge_a, edge_b):
    """Fraction of the (k/2)^2 canonical paths edge_a -> agg(pod_a, a) ->
    core(a+1, i) -> agg(pod_b, a) -> edge_b (one per (agg local index a,
    core column i)) that survive, checked by direct link existence --
    *not* by enumerating nx.all_shortest_paths, which, once every
    canonical path between a pair is broken, instead returns however many
    longer detours happen to exist at the new shortest length (real ones:
    edge_a can reach a *different* local-index agg in its own pod by
    relaying through a sibling edge switch, since edge<->agg is complete
    bipartite within a pod -- see PHASE5.md Step 1 and
    test_path_diversity_detour_does_not_count). None of those detours are
    one of the (k/2)^2 paths this metric is normalized by; counting them
    produced upward noise spikes (observed at small k, e.g. k=16 bouncing
    42%->62%->35% across adjacent failure percentages). A detour isn't
    diversity; it's a sign the original paths are gone.
    """
    half = k // 2
    survivors = 0
    for a in range(half):
        agg_a = pod_switch_name(pod_a, half + a, half)
        agg_b = pod_switch_name(pod_b, half + a, half)
        if not (g_after.has_edge(edge_a, agg_a) and g_after.has_edge(agg_b, edge_b)):
            continue  # the edge->agg / agg->edge legs are shared by every core column
        row_j = a + 1
        survivors += sum(
            1 for i in range(1, half + 1)
            if g_after.has_edge(agg_a, core_switch_name(row_j, i))
            and g_after.has_edge(core_switch_name(row_j, i), agg_b)
        )
    return survivors / (half * half)


def path_diversity(g_after, k, pod_a, pod_b):
    """Mean, over every (edge switch in pod_a, edge switch in pod_b) pair,
    of the same quantity `_canonical_survivor_fraction` computes per pair
    (PHASE5.md Step 1; see that function's docstring for why averaging over
    every pair, and why only canonical paths count). Vectorized with numpy
    instead of calling `_canonical_survivor_fraction` edges_a*edges_b times:
    a naive O((k/2)^4) check per pod-pair (half^2 edge pairs, each
    rechecking half core columns per agg leg) made `cmd_e1_resilience`'s
    k=32 rerun not finish in reasonable time, since it now also averages
    over several pod pairs (`_e1_pod_pairs`) on top of that. The per-(agg
    local index) core survival count doesn't depend on which edge switch is
    asking, so it's computed once per row and reused via a matrix product
    instead of a Python loop -- O((k/2)^3) per pod-pair, ~(k/2)x fewer
    operations, with `test_path_diversity_matches_canonical_survivor_fraction`
    checking the two stay in exact agreement.
    """
    half = k // 2
    edges_a = [n for n, d in g_after.nodes(data=True) if d["type"] == "edge" and d["pod"] == pod_a]
    edges_b = [n for n, d in g_after.nodes(data=True) if d["type"] == "edge" and d["pod"] == pod_b]
    if not edges_a or not edges_b:
        return 0.0

    agg_a_names = [pod_switch_name(pod_a, half + a, half) for a in range(half)]
    agg_b_names = [pod_switch_name(pod_b, half + a, half) for a in range(half)]

    core_survivors = np.array([
        sum(
            1 for i in range(1, half + 1)
            if g_after.has_edge(agg_a_names[a], core_switch_name(a + 1, i))
            and g_after.has_edge(core_switch_name(a + 1, i), agg_b_names[a])
        )
        for a in range(half)
    ], dtype=np.float64)

    edge_a_ok = np.array([[g_after.has_edge(ea, agg_a_names[a]) for a in range(half)] for ea in edges_a],
                          dtype=np.float64)
    edge_b_ok = np.array([[g_after.has_edge(eb, agg_b_names[a]) for a in range(half)] for eb in edges_b],
                          dtype=np.float64)

    survivors = (edge_a_ok * core_survivors) @ edge_b_ok.T  # (len(edges_a), len(edges_b))
    return float(survivors.sum() / (len(edges_a) * len(edges_b)) / (half * half))


def oversubscription_stats(g_after, k):
    """Per-switch (k/2):surviving-uplinks oversubscription, worst-case and mean,
    for the edge and agg layers separately (CLAUDE.md Sec. 6, "Capacity")."""
    half = k // 2

    def ratios_for(switch_type, upstream_type):
        values = []
        for n, d in g_after.nodes(data=True):
            if d["type"] != switch_type:
                continue
            uplinks = sum(1 for nb in g_after.neighbors(n) if g_after.nodes[nb]["type"] == upstream_type)
            values.append(half / uplinks if uplinks else float("inf"))
        return values

    def summarize(ratios):
        finite = [r for r in ratios if r != float("inf")]
        return {
            "worst_case": max(ratios) if ratios else None,
            "mean": (sum(finite) / len(finite)) if finite else None,
            "fully_disconnected_count": sum(1 for r in ratios if r == float("inf")),
        }

    return {
        "edge": summarize(ratios_for("edge", "agg")),
        "agg": summarize(ratios_for("agg", "core")),
    }


def min_pod_to_pod_maxflow(g_after, k, pod_a, pod_b):
    """Max-flow (unit capacities) between pod_a's and pod_b's agg switches, as a
    proxy for remaining bisection bandwidth between that pod pair (CLAUDE.md Sec.
    6, "Capacity"). Healthy max = (k/2)^2."""
    half = k // 2
    g2 = g_after.copy()
    for u, v in g2.edges():
        g2[u][v]["capacity"] = 1
    g2.add_node("_src")
    g2.add_node("_sink")
    # source/sink edges are uncapped (half is already >= any agg switch's real
    # uplink count) so the actual k/2-per-switch uplink edges are what bind.
    for s in range(half, k):
        name = f"agg-p{pod_a}-s{s}"
        if g2.has_node(name):
            g2.add_edge("_src", name, capacity=half)
    for s in range(half, k):
        name = f"agg-p{pod_b}-s{s}"
        if g2.has_node(name):
            g2.add_edge(name, "_sink", capacity=half)
    if not g2.has_node("_src") or not g2.has_node("_sink") or not nx.has_path(g2, "_src", "_sink"):
        return 0
    value, _ = nx.maximum_flow(g2, "_src", "_sink")
    return value
