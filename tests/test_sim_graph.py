import networkx as nx
import pytest

from fattree.failures import apply_failures
from fattree.sim.graph import (
    connectivity_and_path_metrics,
    min_pod_to_pod_maxflow,
    oversubscription_stats,
    path_diversity,
)
from fattree.topology import build_fat_tree, hosts_of


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


# --- path_diversity --------------------------------------------------------------------

def test_path_diversity_is_1_when_healthy():
    g = build_fat_tree(8)
    assert path_diversity(g, 8, 0, 1) == pytest.approx(1.0)


def test_path_diversity_matches_brute_force_count():
    k = 8
    g = build_fat_tree(k)
    g2, _ = _apply(g, link_pct=10, mode="bernoulli", seed=3)
    half = k // 2
    edge_a = next(n for n, d in g2.nodes(data=True) if d["type"] == "edge" and d["pod"] == 0)
    edge_b = next(n for n, d in g2.nodes(data=True) if d["type"] == "edge" and d["pod"] == 1)
    sub = g2.subgraph([n for n, d in g2.nodes(data=True) if d["type"] != "host"])
    bf_count = sum(1 for _ in nx.all_shortest_paths(sub, edge_a, edge_b))
    assert path_diversity(g2, k, 0, 1) == pytest.approx(bf_count / (half * half))


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
