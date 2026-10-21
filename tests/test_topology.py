import networkx as nx
import pytest

from fattree.topology import (
    build_fat_tree,
    build_topology,
    build_traditional_tree,
    validate,
    validate_topology,
    validate_traditional_tree,
)


@pytest.mark.parametrize("k", [4, 6, 8])
def test_counts_match_paper_formulas(k):
    g = build_fat_tree(k)
    validate(g, k)
    hosts = [n for n, d in g.nodes(data=True) if d["type"] == "host"]
    switches = [n for n, d in g.nodes(data=True) if d["type"] != "host"]
    assert len(hosts) == k**3 // 4
    assert len(switches) == 5 * k**2 // 4


@pytest.mark.parametrize("k", [4, 6, 8])
def test_links_per_layer(k):
    g = build_fat_tree(k)
    for layer in ("host-edge", "edge-agg", "agg-core"):
        count = sum(1 for _, _, d in g.edges(data=True) if d["layer"] == layer)
        assert count == k**3 // 4


@pytest.mark.parametrize("bad_k", [3, 5, -4, 0, 4.5])
def test_bad_k_rejected(bad_k):
    with pytest.raises(ValueError):
        build_fat_tree(bad_k)


def test_fig3_connectivity_example():
    # Paper Fig. 3 (k=4): agg switch 10.0.2.1 connects to cores 10.4.1.1 and 10.4.1.2.
    g = build_fat_tree(4)
    agg = "agg-p0-s2"
    assert g.nodes[agg]["ip"] == "10.0.2.1"
    neighbor_ips = {g.nodes[n]["ip"] for n in g.neighbors(agg) if g.nodes[n]["type"] == "core"}
    assert neighbor_ips == {"10.4.1.1", "10.4.1.2"}


def test_addressing_scheme():
    k = 4
    g = build_fat_tree(k)
    for n, d in g.nodes(data=True):
        if d["type"] == "host":
            assert d["ip"] == f"10.{d['pod']}.{d['switch']}.{d['host_id']}"
            assert 2 <= d["host_id"] <= k // 2 + 1
        elif d["type"] in ("edge", "agg"):
            assert d["ip"] == f"10.{d['pod']}.{d['switch']}.1"
        elif d["type"] == "core":
            assert d["ip"] == f"10.{k}.{d['j']}.{d['i']}"
            assert 1 <= d["j"] <= k // 2
            assert 1 <= d["i"] <= k // 2


def test_shortest_host_paths_hop_counts():
    k = 4
    g = build_fat_tree(k)
    by_pod_switch = {}
    for n, d in g.nodes(data=True):
        if d["type"] == "host":
            by_pod_switch.setdefault((d["pod"], d["switch"]), []).append(n)

    # same edge switch -> 2 hops
    same_edge = next(hs for hs in by_pod_switch.values() if len(hs) >= 2)
    assert nx.shortest_path_length(g, same_edge[0], same_edge[1]) == 2

    # same pod, different edge switch -> 4 hops
    pod0_switches = sorted(sw for (pod, sw) in by_pod_switch if pod == 0)
    h_a = by_pod_switch[(0, pod0_switches[0])][0]
    h_b = by_pod_switch[(0, pod0_switches[1])][0]
    assert nx.shortest_path_length(g, h_a, h_b) == 4

    # different pod -> 6 hops
    h_other_pod = by_pod_switch[(1, 0)][0]
    assert nx.shortest_path_length(g, h_a, h_other_pod) == 6


def test_edge_switches_fully_meshed_with_agg_in_pod():
    k = 6
    g = build_fat_tree(k)
    half = k // 2
    pod = 2
    for e in range(half):
        edge_node = f"edge-p{pod}-s{e}"
        agg_neighbors = {n for n in g.neighbors(edge_node) if g.nodes[n]["type"] == "agg"}
        assert len(agg_neighbors) == half


# --- traditional (oversubscribed) tree -------------------------------------------

@pytest.mark.parametrize("k", [4, 6, 8])
def test_traditional_tree_counts(k):
    g = build_traditional_tree(k)
    validate_traditional_tree(g, k)
    half = k // 2
    hosts = [n for n, d in g.nodes(data=True) if d["type"] == "host"]
    edges = [n for n, d in g.nodes(data=True) if d["type"] == "edge"]
    aggs = [n for n, d in g.nodes(data=True) if d["type"] == "agg"]
    cores = [n for n, d in g.nodes(data=True) if d["type"] == "core"]
    # same host count as the fat-tree, for a fair comparison
    assert len(hosts) == k**3 // 4
    assert len(edges) == k * half
    assert len(aggs) == k         # one agg switch per pod (vs k*half/2 in the fat-tree)
    assert len(cores) == 1        # one core switch total (vs (half)**2 in the fat-tree)


@pytest.mark.parametrize("k", [4, 6, 8])
def test_traditional_tree_is_connected(k):
    g = build_traditional_tree(k)
    assert nx.is_connected(g)


def test_traditional_tree_hop_counts_match_fat_tree():
    # Same addressing/hop structure as the fat-tree (2/4/6 hops) -- the
    # difference from the fat-tree is path *redundancy*, not path *length*.
    k = 4
    g = build_traditional_tree(k)
    by_pod_switch = {}
    for n, d in g.nodes(data=True):
        if d["type"] == "host":
            by_pod_switch.setdefault((d["pod"], d["switch"]), []).append(n)

    same_edge = next(hs for hs in by_pod_switch.values() if len(hs) >= 2)
    assert nx.shortest_path_length(g, same_edge[0], same_edge[1]) == 2

    pod0_switches = sorted(sw for (pod, sw) in by_pod_switch if pod == 0)
    h_a = by_pod_switch[(0, pod0_switches[0])][0]
    h_b = by_pod_switch[(0, pod0_switches[1])][0]
    assert nx.shortest_path_length(g, h_a, h_b) == 4

    h_other_pod = by_pod_switch[(1, 0)][0]
    assert nx.shortest_path_length(g, h_a, h_other_pod) == 6


def test_traditional_tree_has_no_redundant_paths():
    # Removing a single edge-agg link isolates that entire edge switch's hosts --
    # unlike the fat-tree, where each edge switch has k/2 parallel uplinks.
    k = 4
    g = build_traditional_tree(k)
    half = k // 2
    edge_node = "edge-p0-s0"
    agg_node = f"agg-p0-s{half}"
    g.remove_edge(edge_node, agg_node)
    host = next(n for n, d in g.nodes(data=True) if d["type"] == "host" and d["pod"] == 0 and d["switch"] == 0)
    other_host = next(n for n, d in g.nodes(data=True)
                       if d["type"] == "host" and not (d["pod"] == 0 and d["switch"] == 0))
    assert not nx.has_path(g, host, other_host)


def test_traditional_tree_rejects_bad_k():
    with pytest.raises(ValueError):
        build_traditional_tree(7)


# --- build_topology / validate_topology dispatch ----------------------------------

def test_build_topology_dispatches_fat_tree():
    g = build_topology(8, "fat-tree")
    validate_topology(g, 8, "fat-tree")
    assert g.number_of_nodes() == 8**3 // 4 + 5 * 8**2 // 4


def test_build_topology_dispatches_traditional_tree():
    g = build_topology(8, "traditional-tree")
    validate_topology(g, 8, "traditional-tree")
    assert sum(1 for _, d in g.nodes(data=True) if d["type"] == "core") == 1


def test_build_topology_rejects_unknown_type():
    with pytest.raises(ValueError):
        build_topology(8, "bogus-type")


def test_validate_topology_rejects_unknown_type():
    g = build_fat_tree(8)
    with pytest.raises(ValueError):
        validate_topology(g, 8, "bogus-type")
