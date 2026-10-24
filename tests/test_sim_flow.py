import copy

import pytest

from fattree.config import DEFAULTS
from fattree.routing import get_router
from fattree.sim.flow import max_min_fair_rates, simulate_flows
from fattree.topology import build_fat_tree
from fattree.traffic import Flow, generate_traffic


# --- max_min_fair_rates: textbook scenarios --------------------------------------------------------------------

def test_single_bottleneck_splits_equally():
    flow_links = {"f1": ["X"], "f2": ["X"], "f3": ["X"]}
    demands = {f: float("inf") for f in flow_links}
    rates = max_min_fair_rates(flow_links, demands, {"X": 3})
    assert rates == {"f1": pytest.approx(1.0), "f2": pytest.approx(1.0), "f3": pytest.approx(1.0)}


def test_classic_asymmetric_two_link_example():
    # f1 uses only A (cap 10); f2 uses A and B; f3 uses only B (cap 2).
    # Max-min fair: B's fair share is 1 (shared by f2, f3); f1 then gets the rest of A (9).
    flow_links = {"f1": ["A"], "f2": ["A", "B"], "f3": ["B"]}
    demands = {f: float("inf") for f in flow_links}
    rates = max_min_fair_rates(flow_links, demands, {"A": 10, "B": 2})
    assert rates["f2"] == pytest.approx(1.0)
    assert rates["f3"] == pytest.approx(1.0)
    assert rates["f1"] == pytest.approx(9.0)


def test_demand_cap_binds_below_link_capacity():
    rates = max_min_fair_rates({"f1": ["X"]}, {"f1": 0.5}, {"X": 1.0})
    assert rates["f1"] == pytest.approx(0.5)


def test_demand_cap_frees_capacity_for_others():
    # f1 is capped at 0.2 (e.g. a slow host); f2 is uncapped and should get the rest.
    flow_links = {"f1": ["X"], "f2": ["X"]}
    rates = max_min_fair_rates(flow_links, {"f1": 0.2, "f2": float("inf")}, {"X": 1.0})
    assert rates["f1"] == pytest.approx(0.2)
    assert rates["f2"] == pytest.approx(0.8)


def test_disjoint_flows_each_get_full_capacity():
    flow_links = {"f1": ["A"], "f2": ["B"]}
    demands = {f: float("inf") for f in flow_links}
    rates = max_min_fair_rates(flow_links, demands, {"A": 1.0, "B": 1.0})
    assert rates["f1"] == pytest.approx(1.0)
    assert rates["f2"] == pytest.approx(1.0)


def test_scalar_link_capacity_applies_to_all_links():
    flow_links = {"f1": ["A", "B"], "f2": ["B"]}
    demands = {f: float("inf") for f in flow_links}
    rates = max_min_fair_rates(flow_links, demands, 1.0)
    # B is shared (fair share 0.5 each); A then has 0.5 spare, but f1 is capped by B anyway
    assert rates["f1"] == pytest.approx(0.5)
    assert rates["f2"] == pytest.approx(0.5)


# --- simulate_flows: integration --------------------------------------------------------------------

def _cfg(k, routing, pattern="random"):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["topology"]["k"] = k
    cfg["routing"] = routing
    cfg["traffic"]["pattern"] = pattern
    return cfg


def test_simulate_flows_basic_structure():
    k = 4
    g = build_fat_tree(k)
    cfg = _cfg(k, "two-level")
    flows = generate_traffic(g, cfg, seed=1)
    router = get_router("two-level", g, cfg)
    result = simulate_flows(router, flows, link_capacity_gbps=0.096, load=1.0)

    assert result["ideal_total_gbps"] == pytest.approx(0.096 * len(flows))
    assert 0 <= result["throughput_pct_of_ideal"] <= 100 + 1e-6
    assert 0 <= result["max_link_load"] <= 1.0 + 1e-9
    assert result["achieved_total_gbps"] >= 0
    assert len(result["rates"]) == len(flows) - len(result["dropped"])


def test_simulate_flows_no_contention_achieves_full_throughput():
    # Two hosts under different edge switches, single flow each via distinct
    # paths at k=8 (plenty of core diversity) -- no real contention expected.
    k = 8
    g = build_fat_tree(k)
    cfg = _cfg(k, "ecmp")
    router = get_router("ecmp", g, cfg)
    hosts = [n for n, d in g.nodes(data=True) if d["type"] == "host"]
    flow = Flow(hosts[0], hosts[-1], 1000, 0.0, "mouse")
    result = simulate_flows(router, [flow], link_capacity_gbps=1.0, load=0.5)
    assert result["rates"][0] == pytest.approx(0.5)
    assert result["throughput_pct_of_ideal"] == pytest.approx(100.0)


def test_simulate_flows_dropped_flow_does_not_crash_and_counts_toward_ideal():
    k = 8
    g = build_fat_tree(k)
    cfg = _cfg(k, "single-path")
    router = get_router("single-path", g, cfg)
    hosts = [n for n, d in g.nodes(data=True) if d["type"] == "host"]
    flow = Flow(hosts[0], hosts[1], 1000, 0.0, "mouse")
    path = router.route(flow)
    dead = (path[1], path[2])
    router.on_failure({dead}, set())

    result = simulate_flows(router, [flow], link_capacity_gbps=1.0, load=0.5)
    assert result["dropped"] == [0]
    assert result["rates"] == {}
    assert result["achieved_total_gbps"] == 0.0
    assert result["ideal_total_gbps"] == pytest.approx(0.5)
    assert result["throughput_pct_of_ideal"] == pytest.approx(0.0)


def test_simulate_flows_incast_is_bottlenecked_at_receiver():
    # Several senders -> one receiver: the receiver's single host-edge link must
    # bottleneck the aggregate achieved rate to its own capacity.
    k = 8
    g = build_fat_tree(k)
    cfg = _cfg(k, "two-level", pattern="incast")
    cfg["traffic"]["incast_senders"] = 10
    flows = generate_traffic(g, cfg, seed=1)
    router = get_router("two-level", g, cfg)
    result = simulate_flows(router, flows, link_capacity_gbps=1.0, load=1.0)
    assert result["achieved_total_gbps"] <= 1.0 + 1e-9  # receiver's link caps the total
