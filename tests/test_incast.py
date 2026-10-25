import copy

import pytest

from analysis import _round_time_and_unfinished, _run_incast_rounds
from fattree.config import DEFAULTS
from fattree.routing import get_router
from fattree.sim.packet import PacketSimulator, simulate_packets
from fattree.sim.packet_metrics import FlowStats
from fattree.topology import build_fat_tree, hosts_of
from fattree.traffic import Flow


def _base_cfg(k=4, queue_size=128, min_rto_ms=200, duration_ms=2000, block_bytes=65_536):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["topology"]["k"] = k
    cfg["routing"] = "two-level"
    cfg["packet"]["transport"] = "tcp_lite"
    cfg["packet"]["queue_size_packets"] = queue_size
    cfg["packet"]["min_rto_ms"] = min_rto_ms
    cfg["packet"]["duration_ms"] = duration_ms
    cfg["incast"]["block_bytes"] = block_bytes
    return cfg


# --- M=1 incast ~ single flow (Part A test 6) --------------------------------------------------------------------

def test_incast_m1_matches_single_flow_goodput():
    k = 4
    g = build_fat_tree(k)
    cfg = _base_cfg(k, queue_size=2000)  # generous queue: see Part A's goodput test note
    router = get_router("two-level", g, cfg)

    incast_stats = _run_incast_rounds(g, router, cfg, m=1, rounds=5, seed=1)

    hosts = hosts_of(g)
    receiver = hosts[0]
    sender = hosts[1]
    router2 = get_router("two-level", g, cfg)
    single_flow = Flow(sender, receiver, cfg["incast"]["block_bytes"], 0.0, "mouse")
    single_result = simulate_packets(g, router2, [single_flow], cfg)
    single_goodput = single_result["flow_stats"][0].goodput_bps()

    assert incast_stats["goodput_bps"] == pytest.approx(single_goodput, rel=0.15)


# --- analytic round-time check, no drops --------------------------------------------------------------------

def test_incast_round_time_no_drops_matches_analytic():
    k = 4
    g = build_fat_tree(k)
    m = 2
    cfg = _base_cfg(k, queue_size=2000)
    router = get_router("two-level", g, cfg)

    stats = _run_incast_rounds(g, router, cfg, m=m, rounds=5, seed=1)
    assert stats["receiver_drops"] == 0

    cap_bps = cfg["link_capacity_gbps"] * 1e9
    block_bytes = cfg["incast"]["block_bytes"]
    # ideal: M blocks arrive back to back at the receiver's own line rate
    # (the actual bottleneck for an incast response), plus modest slow-start/RTT overhead
    analytic_round_ns = m * block_bytes * 8 * 1e9 / cap_bps
    assert stats["mean_round_time_ns"] >= analytic_round_ns
    assert stats["mean_round_time_ns"] <= analytic_round_ns * 3  # generous slack for slow start + multiple senders


# --- conservation (data packets sent == delivered + dropped) on the incast pattern --------------------------------------------------------------------

def test_incast_conservation():
    k = 4
    g = build_fat_tree(k)
    cfg = _base_cfg(k, queue_size=16)  # small: forces some genuine drops
    router = get_router("two-level", g, cfg)
    from fattree.traffic import generate_pairs
    cfg_round = copy.deepcopy(cfg)
    cfg_round["traffic"]["pattern"] = "incast"
    cfg_round["traffic"]["incast_senders"] = 8
    pairs = generate_pairs(g, cfg_round, seed=1)
    flows = [Flow(src, dst, cfg["incast"]["block_bytes"], 0.0, "mouse") for src, dst in pairs]
    sim = PacketSimulator(g, router, flows, cfg_round)
    result = sim.run()
    assert result["total_sent"] == result["total_delivered"] + result["total_dropped"]


# --- round timing with a straggler (PHASE6.md Step 1) --------------------------------------------------------------------

def test_round_time_charges_full_budget_when_a_flow_is_unfinished():
    """A round where one flow finished quickly and another never finished
    must be timed by the simulation's full budget, not by the finished
    flow's (much shorter) FCT -- the old bug made such a round look faster
    than it really was."""
    duration_ns = 50_000_000  # 50ms budget
    fast = FlowStats(0, "mouse", 1000, start_time_ns=0)
    fast.finish(2_000_000)  # finished at 2ms
    slow = FlowStats(1, "mouse", 1000, start_time_ns=0)  # never finished

    round_time_ns, unfinished = _round_time_and_unfinished({0: fast, 1: slow}, duration_ns)
    assert round_time_ns == duration_ns
    assert unfinished == 1


def test_round_time_uses_max_fct_when_everyone_finishes():
    duration_ns = 50_000_000
    a = FlowStats(0, "mouse", 1000, start_time_ns=0)
    a.finish(2_000_000)
    b = FlowStats(1, "mouse", 1000, start_time_ns=0)
    b.finish(5_000_000)

    round_time_ns, unfinished = _round_time_and_unfinished({0: a, 1: b}, duration_ns)
    assert round_time_ns == 5_000_000
    assert unfinished == 0


# --- where-time-goes waiting time is never negative (PHASE6.md Step 1) --------------------------------------------------------------------

def test_incast_waiting_time_never_negative_for_a_small_sweep():
    k = 4
    g = build_fat_tree(k)
    cap_bps = DEFAULTS["link_capacity_gbps"] * 1e9
    for min_rto_ms in (200, 1):
        cfg = _base_cfg(k, queue_size=64, min_rto_ms=min_rto_ms, duration_ms=3000)
        router = get_router("two-level", g, cfg)
        for m in (1, 2, 4):
            stats = _run_incast_rounds(g, router, cfg, m=m, rounds=5, seed=1)
            ideal_send_ns = stats["n_senders"] * cfg["incast"]["block_bytes"] * 8 * 1e9 / cap_bps
            waiting_ns = stats["mean_round_time_ns"] - ideal_send_ns
            assert waiting_ns >= 0, f"min_rto_ms={min_rto_ms} m={m}: waiting_ns={waiting_ns}"


# --- determinism --------------------------------------------------------------------

def test_incast_determinism_same_seed():
    k = 4
    g = build_fat_tree(k)
    cfg = _base_cfg(k, queue_size=32)
    router1 = get_router("two-level", g, cfg)
    router2 = get_router("two-level", g, cfg)

    stats1 = _run_incast_rounds(g, router1, cfg, m=8, rounds=3, seed=7)
    stats2 = _run_incast_rounds(g, router2, cfg, m=8, rounds=3, seed=7)

    assert stats1["goodput_bps"] == stats2["goodput_bps"]
    assert stats1["timeouts_per_round"] == stats2["timeouts_per_round"]
    assert stats1["receiver_drops"] == stats2["receiver_drops"]
