import pytest

from fattree.failures import apply_failures, resolve_pct
from fattree.topology import build_fat_tree


def _cfg(link_pct=0, switch_pct=0, mode="exact",
         link_layers=("edge-agg", "agg-core"), switch_layers=("core", "agg", "edge")):
    return {
        "link_pct": link_pct,
        "switch_pct": switch_pct,
        "mode": mode,
        "link_layers": list(link_layers),
        "switch_layers": list(switch_layers),
    }


def test_exact_mode_count_matches_round():
    g = build_fat_tree(8)
    cfg = _cfg(link_pct=10, mode="exact")
    _, failed = apply_failures(g, cfg, seed=1)
    eligible = sum(1 for _, _, d in g.edges(data=True) if d["layer"] in cfg["link_layers"])
    assert len(failed["failed_links"]) == round(0.10 * eligible)


def test_zero_pct_fails_nothing():
    g = build_fat_tree(8)
    g2, failed = apply_failures(g, _cfg(), seed=1)
    assert len(failed["failed_links"]) == 0
    assert len(failed["failed_switches"]) == 0
    assert g2.number_of_edges() == g.number_of_edges()
    assert g2.number_of_nodes() == g.number_of_nodes()


def test_determinism_same_seed():
    g = build_fat_tree(8)
    cfg = _cfg(link_pct=15, switch_pct=5, mode="exact")
    _, failed1 = apply_failures(g, cfg, seed=42)
    _, failed2 = apply_failures(g, cfg, seed=42)
    assert failed1["failed_links"] == failed2["failed_links"]
    assert failed1["failed_switches"] == failed2["failed_switches"]


def test_different_seed_gives_different_selection():
    g = build_fat_tree(8)
    cfg = _cfg(link_pct=15, mode="exact")
    _, failed1 = apply_failures(g, cfg, seed=1)
    _, failed2 = apply_failures(g, cfg, seed=2)
    assert failed1["failed_links"] != failed2["failed_links"]


def test_switch_removal_removes_node_and_incident_links():
    g = build_fat_tree(8)
    cfg = _cfg(switch_pct=20, switch_layers=["edge"], mode="exact")
    g2, failed = apply_failures(g, cfg, seed=3)
    assert len(failed["failed_switches"]) > 0
    for n in failed["failed_switches"]:
        assert n not in g2
        assert g.nodes[n]["type"] == "edge"


def test_bernoulli_mode_edge_count_consistent():
    g = build_fat_tree(8)
    cfg = _cfg(link_pct=50, mode="bernoulli")
    g2, failed = apply_failures(g, cfg, seed=1)
    assert g2.number_of_edges() == g.number_of_edges() - len(failed["failed_links"])


def test_invalid_mode_raises():
    g = build_fat_tree(8)
    cfg = _cfg(link_pct=10, mode="bogus")
    with pytest.raises(ValueError):
        apply_failures(g, cfg, seed=1)


def test_original_graph_not_mutated():
    g = build_fat_tree(8)
    original_edges = g.number_of_edges()
    apply_failures(g, _cfg(link_pct=50, mode="exact"), seed=1)
    assert g.number_of_edges() == original_edges


def test_resolve_pct_passes_scalars_through():
    import random
    rng = random.Random(1)
    assert resolve_pct(7, rng) == 7


def test_resolve_pct_range_stays_within_bounds():
    import random
    rng = random.Random(1)
    for _ in range(50):
        pct = resolve_pct([5, 15], rng)
        assert 5 <= pct <= 15


def test_pct_range_resolves_and_matches_reported_value():
    g = build_fat_tree(8)
    cfg = _cfg(link_pct=[0, 20], mode="exact")
    _, failed = apply_failures(g, cfg, seed=7)
    eligible = sum(1 for _, _, d in g.edges(data=True) if d["layer"] in cfg["link_layers"])
    assert 0 <= failed["link_pct"] <= 20
    assert len(failed["failed_links"]) == round(failed["link_pct"] / 100 * eligible)


def test_pct_range_determinism_same_seed():
    g = build_fat_tree(8)
    cfg = _cfg(link_pct=[0, 30], switch_pct=[0, 10], mode="exact")
    _, failed1 = apply_failures(g, cfg, seed=99)
    _, failed2 = apply_failures(g, cfg, seed=99)
    assert failed1["link_pct"] == failed2["link_pct"]
    assert failed1["switch_pct"] == failed2["switch_pct"]
    assert failed1["failed_links"] == failed2["failed_links"]
