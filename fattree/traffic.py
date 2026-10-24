"""Traffic generation: patterns (paper Sec. 5.2) and flow-size models.

A `Flow` is (src, dst, size_bytes, start_time, cls): host node names (as in
fattree.topology / fattree.topology.hosts_of), a size in bytes, a start time in
seconds, and cls in {"mouse", "elephant"}. This module only decides *who* sends to
*whom*, how big each flow is, and its class; `traffic.load` (offered load as a
fraction of host link rate) is consumed downstream by the flow/packet simulators
(PHASE2_PROMPT.md Step 4) to turn sizes into rates/timing -- it is not used here.
All flows get start_time=0.0 (a single concurrent snapshot); the packet-level
simulator assigns real inter-arrival times.

Supported `traffic.pattern` values (paper Sec. 5.2):
  random               - random derangement: every host sends to, and receives
                          from, exactly one other host (Sattolo's algorithm:
                          guaranteed no fixed points, unlike plain shuffle+reject)
  stride               - host at ordered index x -> host at index (x + stride) mod N
  staggered            - per src: same edge switch w.p. subnet_p, else same pod
                          w.p. pod_p, else anywhere (paper's local-traffic model)
  same-pod             - a random derangement restricted to each pod separately
  all-to-all           - every ordered (src, dst) pair, src != dst
  incast               - traffic.incast_senders random hosts -> 1 random receiver
  inter-pod-incoming   - paper worst case, (k-1):1: every host in every pod other
                          than one target pod sends to a random host in that pod
  same-id-outgoing     - paper worst case, (k/2):1: every host sends to a random
                          host with the SAME host_id in a different pod
"""
import csv
import random
from collections import namedtuple

from fattree.topology import hosts_of

Flow = namedtuple("Flow", ["src", "dst", "size_bytes", "start_time", "cls"])


def _derangement_sattolo(n, rng):
    """Sattolo's algorithm: a uniformly random single-n-cycle permutation of
    range(n), which by construction has no fixed points (unlike shuffle+reject,
    no rejection/retry loop is needed). Requires n >= 2."""
    perm = list(range(n))
    for i in range(n - 1, 0, -1):
        j = rng.randrange(i)
        perm[i], perm[j] = perm[j], perm[i]
    return perm


def _groups_by(g, hosts, *keys):
    groups = {}
    for h in hosts:
        d = g.nodes[h]
        groups.setdefault(tuple(d[k] for k in keys), []).append(h)
    return groups


def _pattern_random(g, hosts, cfg, rng):
    perm = _derangement_sattolo(len(hosts), rng)
    return [(hosts[i], hosts[perm[i]]) for i in range(len(hosts))]


def _pattern_stride(g, hosts, cfg, rng):
    n = len(hosts)
    stride = cfg["traffic"].get("stride", 1)
    return [(hosts[x], hosts[(x + stride) % n]) for x in range(n)]


def _pattern_staggered(g, hosts, cfg, rng):
    subnet_p = cfg["traffic"].get("subnet_p", 0.5)
    pod_p = cfg["traffic"].get("pod_p", 0.3)
    by_edge = _groups_by(g, hosts, "pod", "switch")
    by_pod = _groups_by(g, hosts, "pod")

    pairs = []
    for src in hosts:
        d = g.nodes[src]
        edge_mates = [h for h in by_edge[(d["pod"], d["switch"])] if h != src]
        pod_mates = [h for h in by_pod[(d["pod"],)] if h != src]
        others = [h for h in hosts if h != src]

        r = rng.random()
        if r < subnet_p and edge_mates:
            dst = rng.choice(edge_mates)
        elif r < subnet_p + pod_p and pod_mates:
            dst = rng.choice(pod_mates)
        else:
            dst = rng.choice(others)
        pairs.append((src, dst))
    return pairs


def _pattern_same_pod(g, hosts, cfg, rng):
    by_pod = _groups_by(g, hosts, "pod")
    pairs = []
    for pod_hosts in by_pod.values():
        if len(pod_hosts) < 2:
            continue
        perm = _derangement_sattolo(len(pod_hosts), rng)
        pairs.extend((pod_hosts[i], pod_hosts[perm[i]]) for i in range(len(pod_hosts)))
    return pairs


def _pattern_all_to_all(g, hosts, cfg, rng):
    return [(src, dst) for src in hosts for dst in hosts if src != dst]


def _pattern_incast(g, hosts, cfg, rng):
    m = cfg["traffic"].get("incast_senders", 8)
    m = min(m, len(hosts) - 1)
    dst = rng.choice(hosts)
    senders = rng.sample([h for h in hosts if h != dst], m)
    return [(src, dst) for src in senders]


def _pattern_inter_pod_incoming(g, hosts, cfg, rng):
    """Paper worst case, (k-1):1: hosts of every pod but one target pod all send
    into that target pod, converging the way a single uplink/core switch would
    see it. Target pod defaults to pod 0 (deterministic) or `traffic.target_pod`."""
    by_pod = _groups_by(g, hosts, "pod")
    pods = sorted(p for (p,) in by_pod)
    target_pod = cfg["traffic"].get("target_pod", pods[0])
    target_hosts = by_pod[(target_pod,)]

    pairs = []
    for pod in pods:
        if pod == target_pod:
            continue
        for src in by_pod[(pod,)]:
            pairs.append((src, rng.choice(target_hosts)))
    return pairs


def _pattern_same_id_outgoing(g, hosts, cfg, rng):
    """Paper worst case, (k/2):1: every host sends to a random host with the same
    host_id in a different pod (two-level routing's ID-based upward port hashes
    these into a shared set of lanes, creating k/2-way contention)."""
    by_id_pod = {}
    for h in hosts:
        d = g.nodes[h]
        by_id_pod.setdefault(d["host_id"], {}).setdefault(d["pod"], []).append(h)

    pairs = []
    for src in hosts:
        d = g.nodes[src]
        same_id_other_pods = by_id_pod[d["host_id"]]
        candidate_pods = [p for p in same_id_other_pods if p != d["pod"]]
        if not candidate_pods:
            continue
        dst_pod = rng.choice(candidate_pods)
        dst = rng.choice(same_id_other_pods[dst_pod])
        pairs.append((src, dst))
    return pairs


_PATTERNS = {
    "random": _pattern_random,
    "stride": _pattern_stride,
    "staggered": _pattern_staggered,
    "same-pod": _pattern_same_pod,
    "all-to-all": _pattern_all_to_all,
    "incast": _pattern_incast,
    "inter-pod-incoming": _pattern_inter_pod_incoming,
    "same-id-outgoing": _pattern_same_id_outgoing,
}


def generate_pairs(g, cfg, seed):
    """Return [(src, dst), ...] host-name pairs for `cfg["traffic"]["pattern"]`."""
    pattern = cfg["traffic"]["pattern"]
    if pattern not in _PATTERNS:
        raise ValueError(f"unknown traffic.pattern: {pattern!r}, must be one of {sorted(_PATTERNS)}")
    hosts = hosts_of(g)
    rng = random.Random(seed)
    return _PATTERNS[pattern](g, hosts, cfg, rng)


def _load_empirical_cdf(path):
    """CSV with columns size_bytes,cdf (cdf strictly increasing in (0, 1], last row = 1.0)."""
    points = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            points.append((float(row["size_bytes"]), float(row["cdf"])))
    points.sort(key=lambda p: p[1])
    return points


def _sample_empirical(points, rng):
    u = rng.random()
    prev_size, prev_cdf = 0.0, 0.0
    for size, cdf in points:
        if u <= cdf:
            if cdf == prev_cdf:
                return size
            frac = (u - prev_cdf) / (cdf - prev_cdf)
            return prev_size + frac * (size - prev_size)
        prev_size, prev_cdf = size, cdf
    return points[-1][0]


def assign_sizes_and_classes(pairs, cfg, seed):
    """Attach size_bytes/start_time/cls to each (src, dst) pair -> list[Flow].

    `traffic.size_model`: fixed | mice-elephants | empirical (config, Step 2).
    """
    traffic_cfg = cfg["traffic"]
    size_model = traffic_cfg.get("size_model", "fixed")
    threshold = traffic_cfg.get("elephant_threshold_bytes", 10_000_000)
    rng = random.Random(seed)

    empirical_points = None
    if size_model == "empirical":
        empirical_points = _load_empirical_cdf(traffic_cfg["empirical_cdf"])

    flows = []
    for src, dst in pairs:
        if size_model == "fixed":
            size = traffic_cfg.get("fixed_size_bytes", 1_000_000)
        elif size_model == "mice-elephants":
            elephant_fraction = traffic_cfg.get("elephant_fraction", 0.1)
            if rng.random() < elephant_fraction:
                lo, hi = traffic_cfg.get("elephant_size_range", (10_000_000, 1_000_000_000))
            else:
                lo, hi = traffic_cfg.get("mouse_size_range", (2_000, 100_000))
            size = rng.uniform(lo, hi)
        elif size_model == "empirical":
            size = _sample_empirical(empirical_points, rng)
        else:
            raise ValueError(f"unknown traffic.size_model: {size_model!r}")

        cls = "elephant" if size >= threshold else "mouse"
        flows.append(Flow(src, dst, size, 0.0, cls))
    return flows


def generate_traffic(g, cfg, seed):
    """Build the full flow set for a topology + config: pattern -> pairs -> sized/classed flows."""
    pairs = generate_pairs(g, cfg, seed)
    return assign_sizes_and_classes(pairs, cfg, seed + 1)
