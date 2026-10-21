import copy

import pytest
import yaml

from fattree.config import DEFAULTS, load_config, validate_config


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
