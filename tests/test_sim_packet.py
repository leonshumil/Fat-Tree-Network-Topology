import copy

import pytest

from fattree.config import DEFAULTS
from fattree.routing import get_router
from fattree.sim.packet import MAX_PACKET_LEVEL_K, PacketSimulator, check_k_allowed, simulate_packets
from fattree.topology import build_fat_tree
from fattree.traffic import Flow


def _cfg(k, routing, **packet_overrides):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["topology"]["k"] = k
    cfg["routing"] = routing
    cfg["packet"].update(packet_overrides)
    return cfg


def _hosts(g):
    return [n for n, d in g.nodes(data=True) if d["type"] == "host"]


# --- k restriction --------------------------------------------------------------------

def test_check_k_allowed_rejects_above_max():
    with pytest.raises(ValueError):
        check_k_allowed(MAX_PACKET_LEVEL_K + 8)


def test_check_k_allowed_accepts_max_and_below():
    check_k_allowed(MAX_PACKET_LEVEL_K)
    check_k_allowed(4)


def test_simulate_packets_rejects_large_k():
    g = build_fat_tree(16)
    cfg = _cfg(16, "ecmp")
    router = get_router("ecmp", g, cfg)
    with pytest.raises(ValueError):
        simulate_packets(g, router, [], cfg)


# --- single flow, no contention: delay = sum(transmission + propagation) ---------------

def test_single_flow_delay_equals_transmission_plus_propagation():
    k = 4
    g = build_fat_tree(k)
    cfg = _cfg(k, "ecmp", duration_ms=50)
    router = get_router("ecmp", g, cfg)
    hosts = _hosts(g)
    flow = Flow(hosts[0], hosts[-1], 500, 0.0, "mouse")  # smaller than MTU: 1 packet
    result = simulate_packets(g, router, [flow], cfg)

    path = router.route(flow)
    cap_bps = cfg["link_capacity_gbps"] * 1e9
    prop = cfg["packet"]["propagation_delay_us"] * 1e-6
    n_hops = len(path) - 1
    expected = n_hops * (500 * 8 / cap_bps + prop)

    assert result["delivered_packets"] == 1
    assert result["dropped_packets"] == 0
    assert result["avg_packet_delay_s"] == pytest.approx(expected)
    assert result["avg_queueing_delay_s"] == pytest.approx(0.0)


def test_queueing_delay_is_nonzero_under_contention():
    result = _spraying_scenario(spraying=False)  # several flows share the same path/queues
    assert result["avg_queueing_delay_s"] > 0


@pytest.mark.parametrize("k", [4, 8])
def test_single_flow_no_contention_no_drops_no_reorder(k):
    g = build_fat_tree(k)
    cfg = _cfg(k, "two-level", duration_ms=50)
    router = get_router("two-level", g, cfg)
    hosts = _hosts(g)
    flow = Flow(hosts[0], hosts[-1], 50_000, 0.0, "mouse")  # multiple packets, same path
    result = simulate_packets(g, router, [flow], cfg)
    assert result["dropped_packets"] == 0
    assert result["reordered_packets"] == 0


# --- queue overflow -> drops --------------------------------------------------------------------

def test_small_queue_under_incast_produces_drops():
    k = 8
    g = build_fat_tree(k)
    cfg = _cfg(k, "two-level", queue_size_packets=2, duration_ms=50)
    cfg["traffic"]["load"] = 1.0
    router = get_router("two-level", g, cfg)
    hosts = _hosts(g)
    dst = hosts[0]
    flows = [Flow(h, dst, 50_000, 0.0, "mouse") for h in hosts[1:9]]  # several senders -> 1 receiver
    result = simulate_packets(g, router, flows, cfg)
    assert result["dropped_packets"] > 0
    assert sum(result["drops_per_link"].values()) == result["dropped_packets"]


def test_large_queue_same_scenario_has_fewer_or_no_drops():
    k = 8
    g = build_fat_tree(k)
    hosts = _hosts(g)
    dst = hosts[0]
    flows = [Flow(h, dst, 50_000, 0.0, "mouse") for h in hosts[1:9]]

    small_cfg = _cfg(k, "two-level", queue_size_packets=2, duration_ms=50)
    small_cfg["traffic"]["load"] = 1.0
    small_result = simulate_packets(g, get_router("two-level", g, small_cfg), flows, small_cfg)

    large_cfg = _cfg(k, "two-level", queue_size_packets=1000, duration_ms=50)
    large_cfg["traffic"]["load"] = 1.0
    large_result = simulate_packets(g, get_router("two-level", g, large_cfg), flows, large_cfg)

    assert large_result["dropped_packets"] <= small_result["dropped_packets"]


# --- reordering: spraying vs per-flow hashing --------------------------------------------------------------------

def _spraying_scenario(spraying, seed=1):
    k = 8
    g = build_fat_tree(k)
    cfg = _cfg(k, "ecmp", duration_ms=50, spraying=spraying)
    cfg["traffic"]["load"] = 1.0
    cfg["seed"] = seed
    router = get_router("ecmp", g, cfg)
    hosts = _hosts(g)
    src, dst = hosts[0], hosts[-1]  # cross-pod: many ECMP paths at k=8
    main_flow = Flow(src, dst, 20_000, 0.0, "mouse")
    # background flows on a single fixed path create uneven per-path congestion,
    # which spraying's random per-packet path choice can then expose as reordering
    bg_router = get_router("single-path", g, _cfg(k, "single-path"))
    bg_flows = [Flow(src, dst, 50_000, 0.0, "mouse") for _ in range(5)]
    return simulate_packets(g, router, [main_flow] + bg_flows, cfg)


def test_per_flow_hashing_never_reorders():
    result = _spraying_scenario(spraying=False)
    assert result["reordered_packets"] == 0


def test_spraying_can_reorder():
    result = _spraying_scenario(spraying=True, seed=1)
    assert result["reordered_packets"] > 0


# --- window transport --------------------------------------------------------------------

def test_window_transport_completes_with_no_loss():
    k = 4
    g = build_fat_tree(k)
    cfg = _cfg(k, "two-level", transport="window", window_packets=4, duration_ms=50)
    router = get_router("two-level", g, cfg)
    hosts = _hosts(g)
    flow = Flow(hosts[0], hosts[-1], 10_000, 0.0, "mouse")  # 7 packets at default MTU
    result = simulate_packets(g, router, [flow], cfg)
    assert result["dropped_packets"] == 0
    assert result["delivered_packets"] == 7
    assert 0 in result["flow_completion_times_s"]


def test_window_transport_recovers_from_loss_via_retransmission():
    # A tiny queue under a large burst forces natural drops; the window
    # transport's timeout-based retransmission must still deliver every packet.
    k = 8
    g = build_fat_tree(k)
    cfg = _cfg(k, "two-level", transport="window", window_packets=20,
               queue_size_packets=2, timeout_ms=1, duration_ms=500)
    router = get_router("two-level", g, cfg)
    hosts = _hosts(g)
    flow = Flow(hosts[0], hosts[-1], 30_000, 0.0, "mouse")  # 20 packets
    result = simulate_packets(g, router, [flow], cfg)
    assert result["dropped_packets"] > 0           # confirms drops genuinely occurred
    assert result["delivered_packets"] == 20        # every packet eventually got through
    assert 0 in result["flow_completion_times_s"]


# --- outputs / determinism --------------------------------------------------------------------

def test_fct_by_class_separates_mice_and_elephants():
    k = 4
    g = build_fat_tree(k)
    cfg = _cfg(k, "two-level", duration_ms=50)
    router = get_router("two-level", g, cfg)
    hosts = _hosts(g)
    mouse = Flow(hosts[0], hosts[1], 2_000, 0.0, "mouse")
    elephant = Flow(hosts[2], hosts[3], 20_000, 0.0, "elephant")
    result = simulate_packets(g, router, [mouse, elephant], cfg)
    assert len(result["fct_by_class_s"]["mouse"]) == 1
    assert len(result["fct_by_class_s"]["elephant"]) == 1


def test_simulate_packets_deterministic_for_seed():
    k = 8
    g = build_fat_tree(k)
    cfg = _cfg(k, "ecmp", duration_ms=50, spraying=True)
    cfg["seed"] = 7
    hosts = _hosts(g)
    flows = [Flow(hosts[0], hosts[-1], 20_000, 0.0, "mouse")]
    r1 = simulate_packets(g, get_router("ecmp", g, cfg), flows, cfg)
    r2 = simulate_packets(g, get_router("ecmp", g, cfg), flows, cfg)
    assert r1 == r2


def test_dropped_flow_has_no_path_counts_in_dropped_flows():
    k = 8
    g = build_fat_tree(k)
    cfg = _cfg(k, "single-path", duration_ms=50)
    router = get_router("single-path", g, cfg)
    hosts = _hosts(g)
    flow = Flow(hosts[0], hosts[1], 1000, 0.0, "mouse")
    path = router.route(flow)
    router.on_failure({(path[1], path[2])}, set())
    result = simulate_packets(g, router, [flow], cfg)
    assert result["dropped_flows"] == [0]
    assert result["delivered_packets"] == 0
