"""Load, merge, and validate the YAML config schema (see config/default.yaml)."""
import copy

import yaml

DEFAULTS = {
    "topology": {"k": 8, "type": "fat-tree"},
    "failures": {
        "link_pct": 0,
        "switch_pct": 0,
        "link_layers": ["edge-agg", "agg-core"],
        "switch_layers": ["core", "agg", "edge"],
        "mode": "exact",
    },
    "traffic": {"pattern": "random", "stride": 1, "load": 0.8},
    "routing": "ecmp",
    "link_capacity_gbps": 1,
    "trials": 30,
    "seed": 1,
    "output_dir": "results",
}

VALID_TOPOLOGY_TYPES = {"fat-tree", "traditional-tree"}
VALID_LINK_LAYERS = {"host-edge", "edge-agg", "agg-core"}
VALID_SWITCH_LAYERS = {"core", "agg", "edge"}
VALID_PATTERNS = {"random", "stride", "all-to-all", "incast", "same-pod", "staggered"}
VALID_ROUTING = {"ecmp", "two-level", "single-path"}
VALID_MODES = {"exact", "bernoulli"}


def _deep_merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path=None):
    """Load config/default.yaml-shaped config, merged over DEFAULTS. `path=None` -> DEFAULTS only."""
    cfg = copy.deepcopy(DEFAULTS)
    if path:
        with open(path) as f:
            user_cfg = yaml.safe_load(f) or {}
        cfg = _deep_merge(cfg, user_cfg)
    validate_config(cfg)
    return cfg


def validate_config(cfg):
    k = cfg["topology"]["k"]
    if not isinstance(k, int) or isinstance(k, bool) or k < 4 or k % 2 != 0:
        raise ValueError(f"topology.k must be an even integer >= 4, got {k!r}")

    topology_type = cfg["topology"].get("type", "fat-tree")
    if topology_type not in VALID_TOPOLOGY_TYPES:
        raise ValueError(f"topology.type must be one of {VALID_TOPOLOGY_TYPES}, got {topology_type!r}")

    for pct_key in ("link_pct", "switch_pct"):
        pct = cfg["failures"][pct_key]
        if isinstance(pct, (list, tuple)):
            if len(pct) != 2:
                raise ValueError(f"failures.{pct_key} range must be [min, max], got {pct!r}")
            lo, hi = pct
            if not (0 <= lo <= hi <= 100):
                raise ValueError(f"failures.{pct_key} range must satisfy 0 <= min <= max <= 100, got {pct!r}")
        elif not (0 <= pct <= 100):
            raise ValueError(f"failures.{pct_key} must be in [0, 100] (or a [min, max] range), got {pct!r}")

    if cfg["failures"]["mode"] not in VALID_MODES:
        raise ValueError(f"failures.mode must be one of {VALID_MODES}, got {cfg['failures']['mode']!r}")

    bad = set(cfg["failures"]["link_layers"]) - VALID_LINK_LAYERS
    if bad:
        raise ValueError(f"failures.link_layers has unknown layers: {bad}")

    bad = set(cfg["failures"]["switch_layers"]) - VALID_SWITCH_LAYERS
    if bad:
        raise ValueError(f"failures.switch_layers has unknown layers: {bad}")

    if cfg["traffic"]["pattern"] not in VALID_PATTERNS:
        raise ValueError(f"traffic.pattern must be one of {VALID_PATTERNS}, got {cfg['traffic']['pattern']!r}")

    if not (0 < cfg["traffic"]["load"] <= 1):
        raise ValueError(f"traffic.load must be in (0, 1], got {cfg['traffic']['load']!r}")

    if cfg["routing"] not in VALID_ROUTING:
        raise ValueError(f"routing must be one of {VALID_ROUTING}, got {cfg['routing']!r}")

    if cfg["trials"] < 1:
        raise ValueError(f"trials must be >= 1, got {cfg['trials']!r}")

    return cfg
