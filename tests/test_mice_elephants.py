import copy

import pytest

from analysis import _precompute_router
from fattree.config import DEFAULTS
from fattree.routing import get_router
from fattree.sim.packet import simulate_packets
from fattree.topology import build_fat_tree, hosts_of
from fattree.traffic import generate_poisson_flows


def _base_cfg(k=4, load=0.3):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["topology"]["k"] = k
    cfg["traffic"]["load"] = load
    cfg["traffic"]["size_model"] = "mice-elephants"
    cfg["traffic"]["elephant_fraction"] = 0.1
    cfg["traffic"]["mouse_size_range"] = [2_000, 100_000]
    cfg["traffic"]["elephant_size_range"] = [1_000_000, 10_000_000]
    cfg["traffic"]["elephant_threshold_bytes"] = 100_000
    cfg["packet"]["transport"] = "tcp_lite"
    cfg["packet"]["min_rto_ms"] = 1
    cfg["packet"]["queue_size_packets"] = 100
    return cfg


# --- low load: every scheme's mean slowdown should be near 1 --------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ["single-path", "ecmp", "two-level"])
def test_low_load_slowdown_near_1(scheme):
    k = 4
    g = build_fat_tree(k)
    cfg = _base_cfg(k, load=0.05)
    cfg["routing"] = scheme
    cfg["packet"]["duration_ms"] = 300

    flows = generate_poisson_flows(g, cfg, duration_s=0.2, seed=1)
    router = get_router(scheme, g, cfg)
    result = simulate_packets(g, router, flows, cfg)

    mtu = cfg["packet"]["mtu_bytes"]
    cap_bps = cfg["link_capacity_gbps"] * 1e9
    prop_ns = cfg["packet"]["propagation_delay_ns"]
    from analysis import _analytic_ideal_fct_ns

    slowdowns = []
    for idx, flow in enumerate(flows):
        fs = result["flow_stats"][idx]
        if fs.fct_ns() is None:
            continue
        path = router.route(flow)
        ideal_ns = _analytic_ideal_fct_ns(path, flow.size_bytes, mtu, cap_bps, prop_ns)
        if ideal_ns:
            slowdowns.append(fs.fct_ns() / ideal_ns)

    assert slowdowns  # sanity: some flows actually completed
    mean_slowdown = sum(slowdowns) / len(slowdowns)
    # Threshold loosened from 1.2 to 3.0 by the Phase 4 go-back-N fix (see
    # fattree/sim/transport.py): with min_rto_ms=1 here, elephants occasionally
    # overflow the queue deep into a large in-flight window; go-back-N then
    # correctly replays the *whole* unacked window from the lost packet
    # onward (RFC 5681, no SACK), not just the one lost packet the pre-fix
    # bug resent. That's more retransmitted bytes per loss, by design -- the
    # old <1.2 bound only held because the bug under-recovered multi-packet
    # bursts (see PHASE4.md Step 1), not because recovery was actually cheap.
    assert mean_slowdown < 3.0


# --- offered load accuracy --------------------------------------------------------------------

@pytest.mark.parametrize("load", [0.3, 0.5])
def test_offered_load_within_tolerance(load):
    k = 8  # more hosts -> more arrivals -> less Poisson sampling noise
    g = build_fat_tree(k)
    cfg = _base_cfg(k, load=load)
    duration_s = 1.0

    flows = generate_poisson_flows(g, cfg, duration_s, seed=1)
    measured_bytes_per_s = sum(f.size_bytes for f in flows) / duration_s
    target_bytes_per_s = load * cfg["link_capacity_gbps"] * 1e9 / 8 * len(hosts_of(g))

    assert measured_bytes_per_s == pytest.approx(target_bytes_per_s, rel=0.05)


# --- byte share by class matches the configured size model --------------------------------------------------------------------

def test_elephant_byte_share_matches_size_model():
    k = 8
    g = build_fat_tree(k)
    cfg = _base_cfg(k, load=0.5)
    cfg["traffic"]["elephant_fraction"] = 0.1
    flows = generate_poisson_flows(g, cfg, duration_s=2.0, seed=1)

    mouse_lo, mouse_hi = cfg["traffic"]["mouse_size_range"]
    elephant_lo, elephant_hi = cfg["traffic"]["elephant_size_range"]
    mean_mouse = (mouse_lo + mouse_hi) / 2
    mean_elephant = (elephant_lo + elephant_hi) / 2
    elephant_fraction = cfg["traffic"]["elephant_fraction"]
    expected_byte_share = (elephant_fraction * mean_elephant) / (
        elephant_fraction * mean_elephant + (1 - elephant_fraction) * mean_mouse)

    elephant_bytes = sum(f.size_bytes for f in flows if f.cls == "elephant")
    total_bytes = sum(f.size_bytes for f in flows)
    measured_byte_share = elephant_bytes / total_bytes

    assert measured_byte_share == pytest.approx(expected_byte_share, abs=0.1)
    # also directly confirms "few flows, most bytes"
    elephant_flow_share = sum(1 for f in flows if f.cls == "elephant") / len(flows)
    assert elephant_flow_share < measured_byte_share


# --- precomputed scheduler/classifier routers produce valid paths --------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ["classifier", "scheduler"])
def test_precompute_router_valid_paths(scheme):
    k = 4
    g = build_fat_tree(k)
    cfg = _base_cfg(k, load=0.3)
    cfg["routing"] = "two-level"
    flows = generate_poisson_flows(g, cfg, duration_s=0.1, seed=1)
    router = _precompute_router(scheme, g, cfg, flows)
    for flow in flows:
        path = router.route(flow)
        if path is None:
            continue
        assert path[0] == flow.src and path[-1] == flow.dst
        for u, v in zip(path, path[1:]):
            assert g.has_edge(u, v)


# --- unfinished flows are reported, never silently dropped from stats --------------------------------------------------------------------

def test_unfinished_flows_tracked_not_silently_dropped():
    k = 4
    g = build_fat_tree(k)
    cfg = _base_cfg(k, load=0.5)
    cfg["routing"] = "two-level"
    cfg["packet"]["duration_ms"] = 5  # deliberately too short for most flows to finish
    flows = generate_poisson_flows(g, cfg, duration_s=0.05, seed=1)
    router = get_router("two-level", g, cfg)
    result = simulate_packets(g, router, flows, cfg)
    assert len(result["flow_stats"]) == len(flows)  # every flow accounted for
    n_unfinished = sum(1 for fs in result["flow_stats"].values() if fs.fct_ns() is None)
    assert n_unfinished > 0
