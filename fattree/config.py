"""Load, merge, and validate the YAML config schema (see config/default.yaml)."""
import copy

import yaml

DEFAULT_ALLOWED_K = [4, 8, 16, 24, 32, 48, 64]

DEFAULTS = {
    "allowed_k": list(DEFAULT_ALLOWED_K),
    "topology": {"k": 8, "type": "fat-tree"},
    "failures": {
        "link_pct": 0,
        "switch_pct": 0,
        "link_layers": ["edge-agg", "agg-core"],
        "switch_layers": ["core", "agg", "edge"],
        "mode": "exact",
    },
    "traffic": {
        "pattern": "random",
        "stride": 1,
        "subnet_p": 0.5,
        "pod_p": 0.3,
        "incast_senders": 8,
        "load": 1.0,
        "size_model": "mice-elephants",
        "fixed_size_bytes": 1_000_000,
        "elephant_fraction": 0.1,
        "elephant_size_range": [10_000_000, 1_000_000_000],
        "mouse_size_range": [2_000, 100_000],
        "elephant_threshold_bytes": 10_000_000,
        "empirical_cdf": None,
    },
    "routing": "two-level",
    "two_level": {"failover": False},
    "classifier": {"rearrange_interval_s": 1.0, "max_moves": 3, "max_rounds": 20},
    "simulation": {"level": "flow"},
    "packet": {
        "mtu_bytes": 1500,
        "ack_bytes": 64,
        "queue_size_packets": 100,
        "propagation_delay_ns": 1000,
        "transport": "tcp_lite",       # udp | tcp_lite
        "init_cwnd": 10,
        "dupack_threshold": 3,         # None disables fast retransmit
        "min_rto_ms": 200,
        "max_rto_ms": None,  # None -> 60 * min_rto_ms (a cap, not in PHASE3_1's spec, that keeps a
                             # connection stuck in repeated loss from backing off past any point of recovery)
        "spraying": False,
        "duration_ms": 100,
        "max_k": 8,
    },
    "incast": {
        "receiver": None,              # None -> first host of pod 0
        "block_bytes": 65_536,
        "rounds": 20,
        "sender_placement": "other-pods",  # other-pods | same-pod | random
    },
    "link_capacity_gbps": 1,
    "trials": 30,
    "seed": 1,
    "output_dir": "results",
}

VALID_TOPOLOGY_TYPES = {"fat-tree", "traditional-tree"}
VALID_LINK_LAYERS = {"host-edge", "edge-agg", "agg-core"}
VALID_SWITCH_LAYERS = {"core", "agg", "edge"}
VALID_PATTERNS = {
    "random", "stride", "all-to-all", "incast", "same-pod", "staggered",
    "inter-pod-incoming", "same-id-outgoing",
}
VALID_SIZE_MODELS = {"fixed", "mice-elephants", "empirical"}
VALID_ROUTING = {"ecmp", "two-level", "single-path", "classifier", "scheduler"}
VALID_MODES = {"exact", "bernoulli"}
VALID_SIM_LEVELS = {"graph", "flow", "packet"}
VALID_TRANSPORTS = {"udp", "tcp_lite"}
VALID_SENDER_PLACEMENTS = {"other-pods", "same-pod", "random"}
PACKET_MAX_K = 8


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


def _format_allowed_k(allowed_k):
    return ", ".join(str(v) for v in sorted(allowed_k))


def validate_k(k, allowed_k):
    """k must be a real switch port count from `allowed_k` (config/default.yaml),
    not just any even integer >= 4 -- PHASE2_PROMPT.md Step 1."""
    if not isinstance(k, int) or isinstance(k, bool):
        raise ValueError(f"topology.k must be an integer, got {k!r}")
    if k not in allowed_k:
        raise ValueError(f"k={k} is not allowed; choose one of {_format_allowed_k(allowed_k)}")


def validate_config(cfg):
    allowed_k = cfg.get("allowed_k", DEFAULT_ALLOWED_K)
    k = cfg["topology"]["k"]
    validate_k(k, allowed_k)

    sweep = cfg.get("sweep")
    if sweep and sweep.get("param") == "topology.k":
        values = sweep.get("values", [])
        bad = [v for v in values if v not in allowed_k]
        if bad:
            raise ValueError(
                f"sweep.values for topology.k must all be in allowed_k "
                f"({_format_allowed_k(allowed_k)}); invalid: {bad}"
            )

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

    traffic = cfg["traffic"]
    if traffic["pattern"] not in VALID_PATTERNS:
        raise ValueError(f"traffic.pattern must be one of {VALID_PATTERNS}, got {traffic['pattern']!r}")

    if not (0 < traffic["load"] <= 1):
        raise ValueError(f"traffic.load must be in (0, 1], got {traffic['load']!r}")

    size_model = traffic.get("size_model", "fixed")
    if size_model not in VALID_SIZE_MODELS:
        raise ValueError(f"traffic.size_model must be one of {VALID_SIZE_MODELS}, got {size_model!r}")
    if size_model == "empirical" and not traffic.get("empirical_cdf"):
        raise ValueError("traffic.size_model is 'empirical' but traffic.empirical_cdf is not set")

    for p_key in ("subnet_p", "pod_p", "elephant_fraction"):
        p = traffic.get(p_key)
        if p is not None and not (0 <= p <= 1):
            raise ValueError(f"traffic.{p_key} must be in [0, 1], got {p!r}")
    if traffic.get("subnet_p", 0) + traffic.get("pod_p", 0) > 1:
        raise ValueError(
            f"traffic.subnet_p + traffic.pod_p must be <= 1, "
            f"got {traffic.get('subnet_p', 0)} + {traffic.get('pod_p', 0)}"
        )

    if traffic.get("incast_senders", 1) < 1:
        raise ValueError(f"traffic.incast_senders must be >= 1, got {traffic.get('incast_senders')!r}")

    if cfg["routing"] not in VALID_ROUTING:
        raise ValueError(f"routing must be one of {VALID_ROUTING}, got {cfg['routing']!r}")

    sim_level = cfg.get("simulation", {}).get("level", "flow")
    if sim_level not in VALID_SIM_LEVELS:
        raise ValueError(f"simulation.level must be one of {VALID_SIM_LEVELS}, got {sim_level!r}")

    packet_cfg = cfg.get("packet", {})
    max_k = packet_cfg.get("max_k", PACKET_MAX_K)
    if sim_level == "packet" and k > max_k:
        raise ValueError(f"simulation.level 'packet' only supports k <= {max_k} (got k={k})")

    transport = packet_cfg.get("transport", "udp")
    if transport not in VALID_TRANSPORTS:
        raise ValueError(f"packet.transport must be one of {VALID_TRANSPORTS}, got {transport!r}")

    sender_placement = cfg.get("incast", {}).get("sender_placement", "other-pods")
    if sender_placement not in VALID_SENDER_PLACEMENTS:
        raise ValueError(f"incast.sender_placement must be one of {VALID_SENDER_PLACEMENTS}, got {sender_placement!r}")

    if cfg["trials"] < 1:
        raise ValueError(f"trials must be >= 1, got {cfg['trials']!r}")

    return cfg
