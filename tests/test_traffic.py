import copy

import pytest

from fattree.config import DEFAULTS
from fattree.topology import build_fat_tree, hosts_of
from fattree.traffic import assign_sizes_and_classes, generate_pairs, generate_traffic


def _cfg(**traffic_overrides):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["traffic"].update(traffic_overrides)
    return cfg


# --- random: derangement + bijection ----------------------------------------------

@pytest.mark.parametrize("k", [4, 8])
def test_random_pattern_is_a_bijection_with_no_fixed_points(k):
    g = build_fat_tree(k)
    hosts = hosts_of(g)
    pairs = generate_pairs(g, _cfg(pattern="random"), seed=1)

    assert len(pairs) == len(hosts)
    srcs = [s for s, _ in pairs]
    dsts = [d for _, d in pairs]
    assert sorted(srcs) == hosts            # every host sends exactly once
    assert sorted(dsts) == hosts            # every host receives exactly once
    assert all(s != d for s, d in pairs)    # derangement: nobody sends to itself


def test_random_pattern_deterministic_per_seed():
    g = build_fat_tree(8)
    cfg = _cfg(pattern="random")
    p1 = generate_pairs(g, cfg, seed=5)
    p2 = generate_pairs(g, cfg, seed=5)
    assert p1 == p2


def test_random_pattern_differs_across_seeds():
    g = build_fat_tree(8)
    cfg = _cfg(pattern="random")
    p1 = generate_pairs(g, cfg, seed=1)
    p2 = generate_pairs(g, cfg, seed=2)
    assert p1 != p2


# --- stride --------------------------------------------------------------------

@pytest.mark.parametrize("stride", [1, 2, 5])
def test_stride_correctness(stride):
    g = build_fat_tree(8)
    hosts = hosts_of(g)
    n = len(hosts)
    pairs = generate_pairs(g, _cfg(pattern="stride", stride=stride), seed=1)
    expected = [(hosts[x], hosts[(x + stride) % n]) for x in range(n)]
    assert pairs == expected


# --- same-pod --------------------------------------------------------------------

def test_same_pod_pairs_never_cross_pods():
    g = build_fat_tree(8)
    pairs = generate_pairs(g, _cfg(pattern="same-pod"), seed=1)
    for src, dst in pairs:
        assert g.nodes[src]["pod"] == g.nodes[dst]["pod"]
        assert src != dst


# --- all-to-all --------------------------------------------------------------------

def test_all_to_all_has_every_ordered_pair():
    g = build_fat_tree(4)
    hosts = hosts_of(g)
    pairs = set(generate_pairs(g, _cfg(pattern="all-to-all"), seed=1))
    expected = {(s, d) for s in hosts for d in hosts if s != d}
    assert pairs == expected
    assert len(pairs) == len(hosts) * (len(hosts) - 1)


# --- incast --------------------------------------------------------------------

def test_incast_single_receiver_distinct_senders():
    g = build_fat_tree(8)
    m = 10
    pairs = generate_pairs(g, _cfg(pattern="incast", incast_senders=m), seed=1)
    dsts = {d for _, d in pairs}
    srcs = [s for s, _ in pairs]
    assert len(dsts) == 1
    assert len(pairs) == m
    assert len(set(srcs)) == m
    assert next(iter(dsts)) not in srcs


def test_incast_default_receiver_is_first_host_of_pod_0():
    g = build_fat_tree(8)
    pairs = generate_pairs(g, _cfg(pattern="incast", incast_senders=4), seed=1)
    dst = pairs[0][1]
    assert dst == hosts_of(g)[0]
    assert g.nodes[dst]["pod"] == 0


def test_incast_sender_placement_other_pods():
    g = build_fat_tree(8)
    cfg = _cfg(pattern="incast", incast_senders=10)
    cfg["incast"] = {"sender_placement": "other-pods"}
    pairs = generate_pairs(g, cfg, seed=1)
    dst_pod = g.nodes[pairs[0][1]]["pod"]
    for src, _ in pairs:
        assert g.nodes[src]["pod"] != dst_pod


def test_incast_sender_placement_same_pod():
    g = build_fat_tree(8)
    cfg = _cfg(pattern="incast", incast_senders=4)
    cfg["incast"] = {"sender_placement": "same-pod"}
    pairs = generate_pairs(g, cfg, seed=1)
    dst_pod = g.nodes[pairs[0][1]]["pod"]
    for src, _ in pairs:
        assert g.nodes[src]["pod"] == dst_pod


def test_incast_explicit_receiver_honored():
    g = build_fat_tree(8)
    explicit_receiver = hosts_of(g)[5]
    cfg = _cfg(pattern="incast", incast_senders=4)
    cfg["incast"] = {"receiver": explicit_receiver}
    pairs = generate_pairs(g, cfg, seed=1)
    assert all(d == explicit_receiver for _, d in pairs)


# --- incast sender-count bug (PHASE6.md Step 1) --------------------------------------------------------------------

def test_incast_requesting_more_senders_than_available_raises():
    """k=4, other-pods placement: only 12 hosts (3 pods x 4 hosts) are
    outside the receiver's pod. Requesting M=15 used to silently create
    only 12 senders (analysis.py then computed the "ideal" time for 15,
    producing a physically impossible >=100% goodput reading) -- it must
    raise instead."""
    g = build_fat_tree(4)
    cfg = _cfg(pattern="incast", incast_senders=15)
    cfg["topology"]["k"] = 4
    cfg["incast"] = {"sender_placement": "other-pods"}
    with pytest.raises(ValueError, match="15 senders but only 12"):
        generate_pairs(g, cfg, seed=1)


def test_incast_exactly_the_available_sender_count_succeeds():
    g = build_fat_tree(4)
    cfg = _cfg(pattern="incast", incast_senders=12)
    cfg["topology"]["k"] = 4
    cfg["incast"] = {"sender_placement": "other-pods"}
    pairs = generate_pairs(g, cfg, seed=1)
    assert len(pairs) == 12
    assert len({src for src, _ in pairs}) == 12


# --- staggered --------------------------------------------------------------------

def test_staggered_subnet_p_1_always_same_edge_switch():
    g = build_fat_tree(8)
    pairs = generate_pairs(g, _cfg(pattern="staggered", subnet_p=1.0, pod_p=0.0), seed=1)
    for src, dst in pairs:
        assert g.nodes[src]["pod"] == g.nodes[dst]["pod"]
        assert g.nodes[src]["switch"] == g.nodes[dst]["switch"]
        assert src != dst


def test_staggered_pod_p_1_always_same_pod_different_edge():
    g = build_fat_tree(8)
    pairs = generate_pairs(g, _cfg(pattern="staggered", subnet_p=0.0, pod_p=1.0), seed=1)
    for src, dst in pairs:
        assert g.nodes[src]["pod"] == g.nodes[dst]["pod"]


# --- inter-pod-incoming --------------------------------------------------------------------

def test_inter_pod_incoming_all_dst_in_target_pod():
    k = 8
    g = build_fat_tree(k)
    target_pod = 2
    pairs = generate_pairs(g, _cfg(pattern="inter-pod-incoming", target_pod=target_pod), seed=1)
    half = k // 2
    hosts_per_pod = half * half
    assert len(pairs) == (k - 1) * hosts_per_pod
    for src, dst in pairs:
        assert g.nodes[dst]["pod"] == target_pod
        assert g.nodes[src]["pod"] != target_pod


# --- same-id-outgoing --------------------------------------------------------------------

def test_same_id_outgoing_preserves_host_id_changes_pod():
    g = build_fat_tree(8)
    pairs = generate_pairs(g, _cfg(pattern="same-id-outgoing"), seed=1)
    assert pairs  # k=8 has multiple pods, so every host has a cross-pod same-ID peer
    for src, dst in pairs:
        assert g.nodes[src]["host_id"] == g.nodes[dst]["host_id"]
        assert g.nodes[src]["pod"] != g.nodes[dst]["pod"]


# --- unknown pattern --------------------------------------------------------------------

def test_unknown_pattern_raises():
    g = build_fat_tree(8)
    with pytest.raises(ValueError):
        generate_pairs(g, _cfg(pattern="bogus"), seed=1)


# --- flow sizing / classes --------------------------------------------------------------------

def test_fixed_size_model():
    g = build_fat_tree(4)
    pairs = generate_pairs(g, _cfg(pattern="random"), seed=1)
    flows = assign_sizes_and_classes(pairs, _cfg(pattern="random", size_model="fixed",
                                                    fixed_size_bytes=42, elephant_threshold_bytes=100), seed=1)
    assert all(f.size_bytes == 42 for f in flows)
    assert all(f.cls == "mouse" for f in flows)  # 42 < threshold 100


def test_mice_elephants_fraction_within_tolerance():
    g = build_fat_tree(16)  # 1024 hosts -> enough samples for a tolerance check
    pairs = generate_pairs(g, _cfg(pattern="random"), seed=1)
    cfg = _cfg(pattern="random", size_model="mice-elephants", elephant_fraction=0.1)
    flows = assign_sizes_and_classes(pairs, cfg, seed=1)
    elephant_frac = sum(1 for f in flows if f.cls == "elephant") / len(flows)
    assert elephant_frac == pytest.approx(0.1, abs=0.05)


def test_elephant_fraction_1_all_elephant_sized_and_classed():
    g = build_fat_tree(4)
    pairs = generate_pairs(g, _cfg(pattern="random"), seed=1)
    cfg = _cfg(pattern="random", size_model="mice-elephants", elephant_fraction=1.0,
               elephant_size_range=[10_000_000, 20_000_000])
    flows = assign_sizes_and_classes(pairs, cfg, seed=1)
    assert all(10_000_000 <= f.size_bytes <= 20_000_000 for f in flows)
    assert all(f.cls == "elephant" for f in flows)


def test_elephant_fraction_0_all_mouse_sized_and_classed():
    g = build_fat_tree(4)
    pairs = generate_pairs(g, _cfg(pattern="random"), seed=1)
    cfg = _cfg(pattern="random", size_model="mice-elephants", elephant_fraction=0.0,
               mouse_size_range=[2_000, 100_000])
    flows = assign_sizes_and_classes(pairs, cfg, seed=1)
    assert all(2_000 <= f.size_bytes <= 100_000 for f in flows)
    assert all(f.cls == "mouse" for f in flows)


def test_empirical_cdf_sampling(tmp_path):
    csv_path = tmp_path / "cdf.csv"
    csv_path.write_text("size_bytes,cdf\n1000,0.5\n2000,1.0\n")
    g = build_fat_tree(4)
    pairs = generate_pairs(g, _cfg(pattern="random"), seed=1)
    cfg = _cfg(pattern="random", size_model="empirical", empirical_cdf=str(csv_path))
    flows = assign_sizes_and_classes(pairs, cfg, seed=1)
    assert all(0 <= f.size_bytes <= 2000 for f in flows)


def test_unknown_size_model_raises():
    g = build_fat_tree(4)
    pairs = generate_pairs(g, _cfg(pattern="random"), seed=1)
    with pytest.raises(ValueError):
        assign_sizes_and_classes(pairs, _cfg(pattern="random", size_model="bogus"), seed=1)


# --- generate_traffic end-to-end + determinism --------------------------------------------------------------------

def test_generate_traffic_deterministic_per_seed():
    g = build_fat_tree(8)
    cfg = _cfg(pattern="random", size_model="mice-elephants")
    f1 = generate_traffic(g, cfg, seed=3)
    f2 = generate_traffic(g, cfg, seed=3)
    assert f1 == f2


def test_generate_traffic_returns_flow_namedtuples_with_expected_fields():
    g = build_fat_tree(4)
    cfg = _cfg(pattern="random")
    flows = generate_traffic(g, cfg, seed=1)
    for f in flows:
        assert f.src in hosts_of(g)
        assert f.dst in hosts_of(g)
        assert f.size_bytes > 0
        assert f.start_time == 0.0
        assert f.cls in ("mouse", "elephant")
