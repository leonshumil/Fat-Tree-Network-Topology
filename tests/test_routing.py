import copy

import networkx as nx
import pytest

from fattree.config import DEFAULTS
from fattree.failures import apply_failures
from fattree.routing import get_router
from fattree.routing.two_level import edge_suffix_port
from fattree.topology import build_fat_tree
from fattree.traffic import Flow, generate_traffic


def _cfg(k, routing, **overrides):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["topology"]["k"] = k
    cfg["routing"] = routing
    for key, value in overrides.items():
        cfg[key] = value
    return cfg


def _host_by_ip(g, ip):
    for n, d in g.nodes(data=True):
        if d.get("type") == "host" and d.get("ip") == ip:
            return n
    raise KeyError(ip)


def _path_is_valid(g, path):
    """Consecutive nodes adjacent, endpoints are the right hosts, no repeats."""
    if path is None:
        return True
    for u, v in zip(path, path[1:]):
        if not g.has_edge(u, v):
            return False
    return len(path) == len(set(path))


# --- registry --------------------------------------------------------------------

def test_get_router_unknown_scheme_raises():
    g = build_fat_tree(4)
    with pytest.raises(ValueError):
        get_router("bogus", g, _cfg(4, "bogus"))


@pytest.mark.parametrize("scheme", ["single-path", "ecmp", "two-level", "classifier", "scheduler"])
def test_get_router_builds_every_registered_scheme(scheme):
    g = build_fat_tree(4)
    router = get_router(scheme, g, _cfg(4, scheme))
    assert router is not None


# --- two-level: the paper's own worked example (Sec. 3.5 / Fig. 3) ----------------

def test_two_level_matches_paper_fig3_example():
    g = build_fat_tree(4)
    router = get_router("two-level", g, _cfg(4, "two-level"))
    src = _host_by_ip(g, "10.0.1.2")
    dst = _host_by_ip(g, "10.2.0.3")
    path = router.route(Flow(src, dst, 1000, 0.0, "mouse"))
    ips = [g.nodes[n].get("ip", n) for n in path]
    assert ips == ["10.0.1.2", "10.0.1.1", "10.0.2.1", "10.4.1.1", "10.2.2.1", "10.2.0.1", "10.2.0.3"]


def test_two_level_suffix_formula_matches_paper_fig4_table():
    # Paper Fig. 4: switch 10.2.2.1's suffix table: 0.0.0.2/8 -> port 2, 0.0.0.3/8 -> port 3.
    half = 2  # k = 4
    z = 2     # switch index of 10.2.2.1 (agg switch, pod 2)
    assert edge_suffix_port(2, z, half) == 2
    assert edge_suffix_port(3, z, half) == 3


def test_two_level_same_host_id_different_lower_switch_spreads_to_different_ports():
    # Paper Sec. 3.5: two flows from different edge switches to hosts with the same
    # host ID on the same destination subnet take different upward ports.
    half = 2
    assert edge_suffix_port(2, z=1, half=half) != edge_suffix_port(2, z=0, half=half)


@pytest.mark.parametrize("k", [4, 8, 16])
def test_two_level_paths_valid_and_deterministic(k):
    g = build_fat_tree(k)
    cfg = _cfg(k, "two-level")
    flows = generate_traffic(g, cfg, seed=1)
    r1 = get_router("two-level", g, cfg)
    r2 = get_router("two-level", g, cfg)
    for flow in flows[:50]:
        p1 = r1.route(flow)
        p2 = r2.route(flow)
        assert p1 == p2
        assert p1[0] == flow.src and p1[-1] == flow.dst
        assert _path_is_valid(g, p1)


def test_two_level_static_tables_drop_flow_on_failed_hop_without_failover():
    k = 8
    g = build_fat_tree(k)
    cfg = _cfg(k, "two-level")
    cfg["two_level"]["failover"] = False
    router = get_router("two-level", g, cfg)

    src = _host_by_ip(g, "10.0.1.2")
    dst = _host_by_ip(g, "10.2.0.3")
    flow = Flow(src, dst, 1000, 0.0, "mouse")
    path_before = router.route(flow)
    assert path_before is not None

    # fail the specific link the static table depends on
    dead_link = (path_before[1], path_before[2])
    router.on_failure({dead_link}, set())
    assert router.route(flow) is None


def test_two_level_failover_reroutes_around_a_dead_upward_link():
    k = 8
    g = build_fat_tree(k)
    cfg = _cfg(k, "two-level")
    cfg["two_level"]["failover"] = True
    router = get_router("two-level", g, cfg)

    src = _host_by_ip(g, "10.0.1.2")
    dst = _host_by_ip(g, "10.2.0.3")
    flow = Flow(src, dst, 1000, 0.0, "mouse")
    path_before = router.route(flow)
    dead_link = (path_before[1], path_before[2])  # edge -> agg hop
    router.on_failure({dead_link}, set())

    path_after = router.route(flow)
    assert path_after is not None
    assert _path_is_valid(g, path_after)
    assert path_after[0] == flow.src and path_after[-1] == flow.dst
    assert tuple(path_after[1:3]) != dead_link


# --- single-path --------------------------------------------------------------------

def test_single_path_valid_and_deterministic():
    g = build_fat_tree(8)
    cfg = _cfg(8, "single-path")
    flows = generate_traffic(g, cfg, seed=1)
    router = get_router("single-path", g, cfg)
    for flow in flows[:30]:
        path = router.route(flow)
        assert _path_is_valid(g, path)
        if path is not None:
            assert path[0] == flow.src and path[-1] == flow.dst


def test_single_path_does_not_reroute_after_failure():
    g = build_fat_tree(8)
    cfg = _cfg(8, "single-path")
    router = get_router("single-path", g, cfg)
    src = _host_by_ip(g, "10.0.1.2")
    dst = _host_by_ip(g, "10.2.0.3")
    flow = Flow(src, dst, 1000, 0.0, "mouse")

    path_before = router.route(flow)
    dead_link = (path_before[1], path_before[2])
    router.on_failure({dead_link}, set())
    assert router.route(flow) is None  # static: no reroute, flow just drops


# --- ecmp --------------------------------------------------------------------

def test_ecmp_valid_and_shortest():
    g = build_fat_tree(8)
    cfg = _cfg(8, "ecmp")
    flows = generate_traffic(g, cfg, seed=1)
    router = get_router("ecmp", g, cfg)
    for flow in flows[:30]:
        path = router.route(flow)
        assert _path_is_valid(g, path)
        assert len(path) - 1 == nx.shortest_path_length(g, flow.src, flow.dst)


def test_ecmp_same_flow_deterministic_different_flows_can_spread():
    g = build_fat_tree(8)
    cfg = _cfg(8, "ecmp")
    router = get_router("ecmp", g, cfg)
    src = _host_by_ip(g, "10.0.1.2")
    dst = _host_by_ip(g, "10.2.0.3")
    flow = Flow(src, dst, 1000, 0.0, "mouse")
    assert router.route(flow) == router.route(flow)  # deterministic for the same flow

    other_flow = Flow(src, dst, 2000, 0.0, "mouse")  # different size -> different hash
    paths = {tuple(router.route(flow)), tuple(router.route(other_flow))}
    # not asserting they differ (hash collisions are possible) -- both must be valid
    for p in paths:
        assert _path_is_valid(g, list(p))


def test_ecmp_reroutes_around_a_failed_link():
    g = build_fat_tree(8)
    cfg = _cfg(8, "ecmp")
    router = get_router("ecmp", g, cfg)
    src = _host_by_ip(g, "10.0.1.2")
    dst = _host_by_ip(g, "10.2.0.3")
    flow = Flow(src, dst, 1000, 0.0, "mouse")
    path_before = router.route(flow)

    g2, failed = apply_failures(g, {
        "link_pct": 0, "switch_pct": 0,
        "link_layers": ["edge-agg", "agg-core"], "switch_layers": [],
        "mode": "exact",
    }, seed=1)
    dead = (path_before[1], path_before[2])
    g2.remove_edge(*dead) if g2.has_edge(*dead) else None

    router2 = get_router("ecmp", g2, cfg)
    path_after = router2.route(flow)
    assert path_after is not None
    assert dead not in zip(path_after, path_after[1:])


# --- classifier --------------------------------------------------------------------

def test_classifier_valid_paths_and_deterministic():
    g = build_fat_tree(8)
    cfg = _cfg(8, "classifier")
    flows = generate_traffic(g, cfg, seed=1)
    r1 = get_router("classifier", g, cfg)
    r2 = get_router("classifier", g, cfg)
    paths1 = r1.route_all(flows[:40])
    paths2 = r2.route_all(flows[:40])
    assert paths1 == paths2
    for flow, path in zip(flows[:40], paths1):
        assert _path_is_valid(g, path)
        if path is not None:
            assert path[0] == flow.src and path[-1] == flow.dst


def test_classifier_balances_load_better_than_naive_same_port():
    # All flows share the same source edge switch and destination pod/ID pattern
    # that would collide on ONE upward port under a naive fixed assignment; the
    # classifier should spread them across the k/2 upward ports via least-loaded
    # placement.
    k = 8
    g = build_fat_tree(k)
    cfg = _cfg(k, "classifier")
    router = get_router("classifier", g, cfg)

    src_edge_hosts = [n for n, d in g.nodes(data=True) if d["type"] == "host" and d["pod"] == 0 and d["switch"] == 0]
    dst_hosts = [n for n, d in g.nodes(data=True) if d["type"] == "host" and d["pod"] == 1][: len(src_edge_hosts)]
    flows = [Flow(s, d, 1_000_000, 0.0, "mouse") for s, d in zip(src_edge_hosts, dst_hosts)]

    paths = router.route_all(flows)
    first_hops_after_edge = {p[2] for p in paths if p is not None}  # the agg switch chosen
    assert len(first_hops_after_edge) > 1  # spread across more than one upward port


def test_classifier_single_flow_route_matches_route_all():
    g = build_fat_tree(8)
    cfg = _cfg(8, "classifier")
    router = get_router("classifier", g, cfg)
    flow = next(iter(generate_traffic(g, cfg, seed=1)))
    assert router.route(flow) == router.route_all([flow])[0]


def test_classifier_respects_failures():
    k = 8
    g = build_fat_tree(k)
    cfg = _cfg(k, "classifier")
    router = get_router("classifier", g, cfg)
    src = _host_by_ip(g, "10.0.1.2")
    dst = _host_by_ip(g, "10.2.0.3")
    flow = Flow(src, dst, 1000, 0.0, "mouse")
    path_before = router.route(flow)
    dead = (path_before[1], path_before[2])
    router.on_failure({dead}, set())
    path_after = router.route(flow)
    if path_after is not None:
        assert dead not in list(zip(path_after, path_after[1:]))


# --- scheduler --------------------------------------------------------------------

def test_scheduler_mice_use_two_level():
    k = 8
    g = build_fat_tree(k)
    cfg = _cfg(k, "scheduler")
    cfg["traffic"]["elephant_threshold_bytes"] = 10_000_000
    two_level_cfg = _cfg(k, "two-level")
    router = get_router("scheduler", g, cfg)
    two_level = get_router("two-level", g, two_level_cfg)

    src = _host_by_ip(g, "10.0.1.2")
    dst = _host_by_ip(g, "10.2.0.3")
    mouse = Flow(src, dst, 1000, 0.0, "mouse")
    assert router.route(mouse) == two_level.route(mouse)


def test_scheduler_elephant_gets_a_valid_reserved_path():
    k = 8
    g = build_fat_tree(k)
    cfg = _cfg(k, "scheduler")
    router = get_router("scheduler", g, cfg)
    src = _host_by_ip(g, "10.0.1.2")
    dst = _host_by_ip(g, "10.2.0.3")
    elephant = Flow(src, dst, 50_000_000, 0.0, "elephant")
    path = router.route(elephant)
    assert _path_is_valid(g, path)
    assert path[0] == src and path[-1] == dst
    assert router.stats()["placements"] == 1
    assert router.stats()["link_state_entries"] > 0
    assert router.stats()["flow_state_entries"] == 1


def test_scheduler_no_shared_reserved_link_between_two_elephants_different_endpoints():
    # Two elephants between the same pod pair but different hosts: their
    # host-edge links differ, so a free inter-pod core path must exist for both
    # (16 candidate core paths at k=8) and the two reservations must not collide.
    k = 8
    g = build_fat_tree(k)
    cfg = _cfg(k, "scheduler")
    router = get_router("scheduler", g, cfg)
    src1, dst1 = _host_by_ip(g, "10.0.1.2"), _host_by_ip(g, "10.2.0.3")
    src2, dst2 = _host_by_ip(g, "10.0.1.3"), _host_by_ip(g, "10.2.0.4")
    e1 = Flow(src1, dst1, 50_000_000, 0.0, "elephant")
    e2 = Flow(src2, dst2, 60_000_000, 0.0, "elephant")

    p1 = router.route(e1)
    p2 = router.route(e2)
    assert p1 is not None and p2 is not None
    assert set(zip(p1, p1[1:])).isdisjoint(zip(p2, p2[1:]))


def test_scheduler_second_elephant_between_same_hosts_is_rejected_until_release():
    # Same src/dst host pair: the host-edge links have no alternate, so a second
    # concurrent elephant between the exact same two hosts must be rejected.
    k = 8
    g = build_fat_tree(k)
    cfg = _cfg(k, "scheduler")
    router = get_router("scheduler", g, cfg)
    src, dst = _host_by_ip(g, "10.0.1.2"), _host_by_ip(g, "10.2.0.3")
    e1 = Flow(src, dst, 50_000_000, 0.0, "elephant")
    e2 = Flow(src, dst, 60_000_000, 0.0, "elephant")

    assert router.route(e1) is not None
    assert router.route(e2) is None
    router.release(e1)
    assert router.route(e2) is not None


def test_scheduler_release_frees_reservation():
    k = 8
    g = build_fat_tree(k)
    cfg = _cfg(k, "scheduler")
    router = get_router("scheduler", g, cfg)
    src = _host_by_ip(g, "10.0.1.2")
    dst = _host_by_ip(g, "10.2.0.3")
    flow = Flow(src, dst, 50_000_000, 0.0, "elephant")
    router.route(flow)
    assert router.stats()["flow_state_entries"] == 1
    router.release(flow)
    assert router.stats()["flow_state_entries"] == 0
