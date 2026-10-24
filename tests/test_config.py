import copy

import pytest
import yaml

from fattree.config import DEFAULT_ALLOWED_K, DEFAULTS, load_config, validate_config, validate_k


def test_defaults_load_without_file():
    cfg = load_config(None)
    assert cfg["topology"]["k"] == DEFAULTS["topology"]["k"]
    assert cfg["topology"]["type"] == "fat-tree"
    assert cfg["failures"]["link_pct"] == DEFAULTS["failures"]["link_pct"]


def test_default_yaml_file_loads():
    cfg = load_config("config/default.yaml")
    assert cfg["topology"]["k"] == 8
    assert cfg["failures"]["link_pct"] == 1


def test_file_overrides_merge(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.dump({"topology": {"k": 16}, "failures": {"link_pct": 25}}))
    cfg = load_config(str(p))
    assert cfg["topology"]["k"] == 16
    assert cfg["failures"]["link_pct"] == 25
    # untouched keys keep their defaults
    assert cfg["failures"]["switch_pct"] == DEFAULTS["failures"]["switch_pct"]
    assert cfg["routing"] == DEFAULTS["routing"]


def test_odd_k_rejected():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["topology"]["k"] = 7
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_pct_out_of_range_rejected():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["failures"]["link_pct"] = 150
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_pct_range_accepted():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["failures"]["link_pct"] = [0, 20]
    cfg["failures"]["switch_pct"] = [5, 5]
    validate_config(cfg)  # should not raise


def test_pct_range_wrong_order_rejected():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["failures"]["link_pct"] = [20, 0]
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_pct_range_out_of_bounds_rejected():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["failures"]["link_pct"] = [0, 150]
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_unknown_pattern_rejected():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["traffic"]["pattern"] = "nonsense"
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_unknown_routing_rejected():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["routing"] = "nonsense"
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_traditional_tree_type_accepted():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["topology"]["type"] = "traditional-tree"
    validate_config(cfg)  # should not raise


def test_unknown_topology_type_rejected():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["topology"]["type"] = "nonsense"
    with pytest.raises(ValueError):
        validate_config(cfg)


# --- allowed_k (PHASE2_PROMPT.md Step 1) ------------------------------------------

def test_default_allowed_k_list():
    assert DEFAULT_ALLOWED_K == [4, 8, 16, 24, 32, 48, 64]


@pytest.mark.parametrize("k", DEFAULT_ALLOWED_K)
def test_allowed_k_values_accepted(k):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["topology"]["k"] = k
    validate_config(cfg)  # should not raise


@pytest.mark.parametrize("bad_k", [25, 47, 2, 3, 5, 0, -4, 20])
def test_disallowed_k_values_rejected(bad_k):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["topology"]["k"] = bad_k
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_disallowed_k_error_message_matches_spec_example():
    with pytest.raises(ValueError, match=r"k=25 is not allowed; choose one of 4, 8, 16, 24, 32, 48, 64"):
        validate_k(25, DEFAULT_ALLOWED_K)


def test_custom_allowed_k_list_is_respected():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["allowed_k"] = [4, 6, 8]
    cfg["topology"]["k"] = 6
    validate_config(cfg)  # 6 is not in DEFAULT_ALLOWED_K but is in this config's own list

    cfg["topology"]["k"] = 16
    with pytest.raises(ValueError):
        validate_config(cfg)  # 16 is not in this config's custom allowed_k


# --- sweep.param = topology.k validation ------------------------------------------

def test_sweep_values_subset_of_allowed_k_accepted():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["sweep"] = {"param": "topology.k", "values": [4, 8, 16]}
    validate_config(cfg)  # should not raise


def test_sweep_values_with_disallowed_k_rejected():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["sweep"] = {"param": "topology.k", "values": [4, 8, 25]}
    with pytest.raises(ValueError, match="25"):
        validate_config(cfg)


def test_sweep_over_other_param_ignores_allowed_k():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["sweep"] = {"param": "failures.link_pct", "values": [0, 1, 2, 5, 10, 15, 20, 30]}
    validate_config(cfg)  # should not raise: allowed_k only constrains topology.k sweeps
