import networkx as nx
import pytest

from fattree.failures import apply_failures
from fattree.sim.graph import (
    _canonical_survivor_fraction,
    connectivity_and_path_metrics,
    min_pod_to_pod_maxflow,
    oversubscription_stats,
    path_diversity,
)
from fattree.topology import build_fat_tree, core_switch_name, hosts_of, pod_switch_name


def _brute_force_metrics(g_before, g_after):
    hosts = hosts_of(g_before)
    ideal = dict(nx.all_pairs_shortest_path_length(g_before))
    total_pairs = connected = weighted_hops = weighted_ideal = max_hops = 0
    for h1 in hosts:
        for h2 in hosts:
            if h1 == h2:
                continue
            total_pairs += 1
            if g_after.has_node(h1) and g_after.has_node(h2) and nx.has_path(g_after, h1, h2):
                d = nx.shortest_path_length(g_after, h1, h2)
                connected += 1
                weighted_hops += d
                weighted_ideal += ideal[h1][h2]
                max_hops = max(max_hops, d)
    isolated = sum(1 for h in hosts if not (g_after.has_node(h) and g_after.degree(h) > 0))
    return {
        "fraction_connected": connected / total_pairs,
        "isolated_hosts": isolated,
        "avg_path_length": weighted_hops / connected if connected else None,
        "avg_stretch": (weighted_hops - weighted_ideal) / connected if connected else None,
        "diameter": max_hops if connected else None,
    }


def _apply(g, **kwargs):
    cfg = {
        "link_pct": 0, "switch_pct": 0,
        "link_layers": ["edge-agg", "agg-core"], "switch_layers": ["core", "agg", "edge"],
        "mode": "exact",
    }
    cfg.update(kwargs)
    return apply_failures(g, cfg, seed=kwargs.get("seed", 1))


@pytest.mark.parametrize("k", [4, 6, 8])
def test_connectivity_metrics_no_failures_match_paper_hop_counts(k):
    g = build_fat_tree(k)
    g2, _ = _apply(g)
    m = connectivity_and_path_metrics(g, g2)
    assert m["fraction_connected"] == pytest.approx(1.0)
    assert m["isolated_hosts"] == 0
    assert m["avg_stretch"] == pytest.approx(0.0)
    assert m["diameter"] == 6


@pytest.mark.parametrize(
    "k,link_pct,switch_pct,mode,seed",
    [
        (4, 10, 0, "bernoulli", 1),
        (4, 5, 5, "exact", 2),
        (4, 20, 0, "exact", 3),
        (6, 10, 0, "bernoulli", 1),
        (8, 5, 5, "exact", 4),
        (8, 15, 0, "exact", 5),
    ],
)
def test_connectivity_metrics_match_brute_force_under_failures(k, link_pct, switch_pct, mode, seed):
    g = build_fat_tree(k)
    g2, _ = _apply(g, link_pct=link_pct, switch_pct=switch_pct,
                   switch_layers=["edge", "agg"], mode=mode, seed=seed)
    bf = _brute_force_metrics(g, g2)
    fn = connectivity_and_path_metrics(g, g2)
    for key in bf:
        if bf[key] is None:
            assert fn[key] is None
        else:
            assert fn[key] == pytest.approx(bf[key], abs=1e-9)


def test_connectivity_metrics_total_disconnection():
    g = build_fat_tree(4)
    g2, _ = _apply(g, switch_pct=100, switch_layers=["core"])
    m = connectivity_and_path_metrics(g, g2)
    assert m["fraction_connected"] < 1.0


# --- path_diversity (PHASE5.md Step 1) --------------------------------------------------------------------

@pytest.mark.parametrize("k", [4, 8, 16])
def test_path_diversity_is_1_when_healthy(k):
    g = build_fat_tree(k)
    assert path_diversity(g, k, 0, 1) == pytest.approx(1.0)


def test_path_diversity_matches_canonical_survivor_fraction():
    """path_diversity's vectorized numpy implementation (needed for
    performance at k=32, see its docstring) must agree exactly with the
    simple per-pair _canonical_survivor_fraction it's a faster version of."""
    k = 8
    g = build_fat_tree(k)
    g2, _ = _apply(g, link_pct=25, mode="bernoulli", switch_layers=[], seed=5)
    edges_a = [n for n, d in g2.nodes(data=True) if d["type"] == "edge" and d["pod"] == 0]
    edges_b = [n for n, d in g2.nodes(data=True) if d["type"] == "edge" and d["pod"] == 1]
    expected = sum(
        _canonical_survivor_fraction(g2, k, 0, 1, ea, eb) for ea in edges_a for eb in edges_b
    ) / (len(edges_a) * len(edges_b))
    assert path_diversity(g2, k, 0, 1) == pytest.approx(expected)


def test_path_diversity_matches_brute_force_length4_simple_paths():
    """Independent cross-check: count simple paths of exactly length 4
    (switch hops) via nx.all_simple_paths with cutoff=4 -- a different
    enumeration technique than _canonical_survivor_fraction's direct link
    checks, filtered to exclude anything longer (a detour)."""
    k = 8
    g = build_fat_tree(k)
    g2, _ = _apply(g, link_pct=10, mode="bernoulli", seed=3)
    half = k // 2
    sub = g2.subgraph([n for n, d in g2.nodes(data=True) if d["type"] != "host"])
    edges_a = [n for n, d in g2.nodes(data=True) if d["type"] == "edge" and d["pod"] == 0]
    edges_b = [n for n, d in g2.nodes(data=True) if d["type"] == "edge" and d["pod"] == 1]

    fractions = []
    for edge_a in edges_a:
        for edge_b in edges_b:
            if edge_a not in sub or edge_b not in sub:
                fractions.append(0.0)
                continue
            count = sum(1 for p in nx.all_simple_paths(sub, edge_a, edge_b, cutoff=4) if len(p) - 1 == 4)
            fractions.append(count / (half * half))
    expected = sum(fractions) / len(fractions)
    assert path_diversity(g2, k, 0, 1) == pytest.approx(expected)


def test_path_diversity_hand_built_single_link_removed():
    """k=4: remove one agg-core link used by pod 0 -> pod 1 paths. That
    link is shared by every edge-switch pair (both pods' edge<->agg legs
    are untouched), so exactly 1 of the 4 canonical paths is lost for
    every pair -- the whole metric drops to exactly 3/4."""
    k = 4
    g = build_fat_tree(k)
    half = k // 2
    g2 = g.copy()
    agg0 = pod_switch_name(0, half, half)
    core = core_switch_name(1, 1)
    g2.remove_edge(agg0, core)
    assert path_diversity(g2, k, 0, 1) == pytest.approx(0.75)


def test_path_diversity_detour_does_not_count():
    """k=4: cut edge_a's link to its own pod's local-index-0 agg, and
    edge_b's link to its own pod's local-index-1 agg. All 4 canonical
    paths between this specific (edge_a, edge_b) pair are now broken --
    but a length-6 detour survives (edge_a -> its other agg -> core ->
    edge_b's pod's matching agg -> a SIBLING edge switch -> edge_b's other
    agg -> edge_b, using the sibling as a relay since edge<->agg is
    complete bipartite within a pod). The canonical fraction for this pair
    must be exactly 0 despite that detour existing."""
    k = 4
    g = build_fat_tree(k)
    half = k // 2
    edge_a, edge_b = "edge-p0-s0", "edge-p1-s0"
    agg_a0 = pod_switch_name(0, half, half)      # local index 0, pod 0
    agg_b1 = pod_switch_name(1, half + 1, half)  # local index 1, pod 1

    g2 = g.copy()
    g2.remove_edge(edge_a, agg_a0)
    g2.remove_edge(edge_b, agg_b1)

    sub = g2.subgraph([n for n, d in g2.nodes(data=True) if d["type"] != "host"])
    assert nx.shortest_path_length(sub, edge_a, edge_b) == 6  # sanity: the detour is real

    assert _canonical_survivor_fraction(g2, k, 0, 1, edge_a, edge_b) == 0.0


def test_path_diversity_monte_carlo_matches_theory():
    k, p, trials = 8, 0.2, 200
    g = build_fat_tree(k)
    vals = []
    for t in range(trials):
        # switch_layers=[] -- no eligible switches to draw bernoulli samples
        # for, so the RNG isn't advanced by irrelevant switch_pct=0 checks
        # before it draws the link failures actually being tested here.
        g2, _ = _apply(g, link_pct=100 * p, mode="bernoulli", switch_layers=[], seed=1000 + t)
        vals.append(path_diversity(g2, k, 0, 1))
    mean = sum(vals) / len(vals)
    assert mean == pytest.approx((1 - p) ** 4, abs=0.02)


# --- oversubscription_stats --------------------------------------------------------------------

def test_oversubscription_healthy_is_1_everywhere():
    g = build_fat_tree(8)
    stats = oversubscription_stats(g, 8)
    assert stats["edge"]["worst_case"] == pytest.approx(1.0)
    assert stats["edge"]["mean"] == pytest.approx(1.0)
    assert stats["agg"]["worst_case"] == pytest.approx(1.0)
    assert stats["agg"]["fully_disconnected_count"] == 0


def test_oversubscription_worsens_after_uplink_failure():
    k = 8
    g = build_fat_tree(k)
    half = k // 2
    g2 = g.copy()
    edge_node = f"edge-p0-s0"
    agg_node = f"agg-p0-s{half}"
    g2.remove_edge(edge_node, agg_node)
    stats = oversubscription_stats(g2, k)
    assert stats["edge"]["worst_case"] > 1.0


# --- min_pod_to_pod_maxflow --------------------------------------------------------------------

@pytest.mark.parametrize("k", [4, 8])
def test_maxflow_healthy_equals_half_squared(k):
    g = build_fat_tree(k)
    half = k // 2
    assert min_pod_to_pod_maxflow(g, k, 0, 1) == half * half


def test_maxflow_drops_after_removing_a_core_switch():
    k = 8
    g = build_fat_tree(k)
    half = k // 2
    g2 = g.copy()
    g2.remove_node("core-1-1")
    flow = min_pod_to_pod_maxflow(g2, k, 0, 1)
    assert flow == half * half - 1


def test_maxflow_zero_when_pod_fully_isolated():
    k = 4
    g = build_fat_tree(k)
    half = k // 2
    g2 = g.copy()
    for s in range(half, k):
        g2.remove_node(f"agg-p0-s{s}")
    assert min_pod_to_pod_maxflow(g2, k, 0, 1) == 0
