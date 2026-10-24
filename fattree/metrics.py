"""Network metrics (see CLAUDE.md Sec. 6 for the full planned list).

Currently implements the assignment's "intermediate" example metric (Sec. 5):
number of switches with multiple failed links, at a fixed failure rate, as a
function of switch port count k -- plus its closed-form sanity check.
"""
import math

from fattree.failures import apply_failures


def switches_with_multiple_failed_links(g, failed_links, min_failed=2):
    """Count switches (excluding hosts) with >= min_failed incident links in failed_links.

    `g` is the topology BEFORE failures were removed (so every switch still has its
    full k incident links to check against). `failed_links` is the set of (u, v)
    tuples returned by fattree.failures.apply_failures.
    """
    failed_set = {frozenset(e) for e in failed_links}
    count = 0
    for n, d in g.nodes(data=True):
        if d["type"] == "host":
            continue
        incident_failed = sum(1 for nb in g.neighbors(n) if frozenset((n, nb)) in failed_set)
        if incident_failed >= min_failed:
            count += 1
    return count


def theoretical_multifail_fraction(k, p, min_failed=2):
    """P(>= min_failed of a switch's k ports fail), each independently at rate p.

    Every switch in the fat-tree has exactly k ports (paper Sec. 3). Closed form
    for min_failed=2 (CLAUDE.md Sec. 6, assignment Sec. 5 "intermediate" example):
      1 - (1-p)^k - k*p*(1-p)^(k-1)
    i.e. 1 - P(0 fail) - P(exactly 1 fails).
    """
    if min_failed != 2:
        raise NotImplementedError("only the >=2 closed form is implemented")
    return 1 - (1 - p) ** k - k * p * (1 - p) ** (k - 1)


def simulate_multifail_vs_k(g, k, link_pct, trials, seed, min_failed=2):
    """Run `trials` independent seeded failure draws (bernoulli, all link layers)
    on `g` and return mean/std/95%-CI switches-with->=min_failed-failed-links,
    plus the closed-form theoretical count for comparison.

    Uses bernoulli mode across host-edge/edge-agg/agg-core so every switch's k
    ports are equally eligible to fail, matching theoretical_multifail_fraction's
    assumption of independent per-port failure.
    """
    failures_cfg = {
        "link_pct": link_pct,
        "switch_pct": 0,
        "link_layers": ["host-edge", "edge-agg", "agg-core"],
        "switch_layers": [],
        "mode": "bernoulli",
    }
    total_switches = sum(1 for _, d in g.nodes(data=True) if d["type"] != "host")

    counts = []
    for t in range(trials):
        _, failed = apply_failures(g, failures_cfg, seed=seed + t)
        counts.append(switches_with_multiple_failed_links(g, failed["failed_links"], min_failed))

    mean = sum(counts) / len(counts)
    var = sum((c - mean) ** 2 for c in counts) / max(1, len(counts) - 1)
    std = math.sqrt(var)
    ci95 = 1.96 * std / math.sqrt(len(counts))
    theory = theoretical_multifail_fraction(k, link_pct / 100, min_failed) * total_switches

    return {
        "k": k,
        "total_switches": total_switches,
        "trials": trials,
        "mean": mean,
        "std": std,
        "ci95": ci95,
        "theory": theory,
    }
