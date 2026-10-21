from itertools import product

import pytest

from fattree.metrics import (
    simulate_multifail_vs_k,
    switches_with_multiple_failed_links,
    theoretical_multifail_fraction,
)
from fattree.topology import build_fat_tree


def test_theoretical_formula_matches_brute_force_k4():
    # Brute force: enumerate all 2^4 failure patterns on a 4-port switch, sum the
    # probability mass where >= 2 of the 4 (independent) ports fail.
    k, p = 4, 0.3
    total_prob = 0.0
    for outcome in product([0, 1], repeat=k):  # 1 = failed
        if sum(outcome) >= 2:
            prob = 1.0
            for bit in outcome:
                prob *= p if bit else (1 - p)
            total_prob += prob
    assert theoretical_multifail_fraction(k, p) == pytest.approx(total_prob, rel=1e-9)


@pytest.mark.parametrize("k,p", [(4, 0.01), (6, 0.1), (8, 0.5)])
def test_theoretical_formula_matches_brute_force_various(k, p):
    total_prob = 0.0
    for outcome in product([0, 1], repeat=k):
        if sum(outcome) >= 2:
            prob = 1.0
            for bit in outcome:
                prob *= p if bit else (1 - p)
            total_prob += prob
    assert theoretical_multifail_fraction(k, p) == pytest.approx(total_prob, rel=1e-9)


def test_theoretical_fraction_boundary_values():
    assert theoretical_multifail_fraction(8, 0.0) == pytest.approx(0.0)
    assert theoretical_multifail_fraction(8, 1.0) == pytest.approx(1.0)


def test_theoretical_fraction_rejects_other_min_failed():
    with pytest.raises(NotImplementedError):
        theoretical_multifail_fraction(8, 0.1, min_failed=3)


def test_switches_with_multiple_failed_links_counts_correctly():
    g = build_fat_tree(4)
    edge_node = "edge-p0-s0"
    neighbors = list(g.neighbors(edge_node))
    assert len(neighbors) >= 2
    # fail exactly 2 of this switch's links -> it should be the only one counted
    failed_links = {(edge_node, neighbors[0]), (edge_node, neighbors[1])}
    assert switches_with_multiple_failed_links(g, failed_links) == 1


def test_switches_with_multiple_failed_links_ignores_single_failure():
    g = build_fat_tree(4)
    edge_node = "edge-p0-s0"
    neighbor = next(iter(g.neighbors(edge_node)))
    failed_links = {(edge_node, neighbor)}
    assert switches_with_multiple_failed_links(g, failed_links) == 0


def test_switches_with_multiple_failed_links_ignores_hosts():
    g = build_fat_tree(4)
    # a host only has 1 link, so it can never reach min_failed=2, but make sure
    # hosts are excluded from the count even in principle
    host = next(n for n, d in g.nodes(data=True) if d["type"] == "host")
    assert switches_with_multiple_failed_links(g, set(g.edges(host))) == 0


def test_simulate_multifail_vs_k_tracks_theory_at_k8():
    g = build_fat_tree(8)
    result = simulate_multifail_vs_k(g, k=8, link_pct=5, trials=50, seed=1)
    # generous tolerance: Monte Carlo mean should land within a few CI widths of theory
    assert result["mean"] == pytest.approx(result["theory"], abs=max(2 * result["ci95"], 1.0))


def test_simulate_multifail_vs_k_deterministic_for_seed():
    g = build_fat_tree(8)
    r1 = simulate_multifail_vs_k(g, k=8, link_pct=5, trials=20, seed=3)
    r2 = simulate_multifail_vs_k(g, k=8, link_pct=5, trials=20, seed=3)
    assert r1["mean"] == r2["mean"]
