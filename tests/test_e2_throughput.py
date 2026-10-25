"""Phase 4, E2 (PHASE4.md Step 5): an independent sanity check for the
collision-ceiling result, deliberately *not* sharing code with
fattree/routing/ecmp.py or fattree/sim/flow.py -- path enumeration uses
networkx's own nx.all_shortest_paths (not the scipy-based rewrite in
fattree/routing/ecmp.py) with true random choice (not the flow-hash
selection ECMP uses), and rate allocation is a freshly-written progressive
filling solver (not fattree/sim/flow.py's heap-based one). If this
independent path lands near the same number as the main pipeline, that is
real evidence the pipeline isn't just internally consistent with itself.
"""
import random

import networkx as nx
import pytest

from fattree.topology import build_fat_tree, hosts_of


def _independent_random_permutation(hosts, rng):
    """A derangement via reject-and-retry -- a different algorithm than
    fattree/traffic.py's Sattolo's-algorithm derangement, generating the
    same kind of "every host sends to, and receives from, exactly one
    other host" traffic matrix a different way."""
    n = len(hosts)
    while True:
        perm = list(range(n))
        rng.shuffle(perm)
        if all(perm[i] != i for i in range(n)):
            return [(hosts[i], hosts[perm[i]]) for i in range(n)]


def _independent_max_min_fair(flow_links, capacity=1.0, demand=1.0):
    """A basic, freshly-written progressive-filling solver: repeatedly
    find the most congested link among still-active flows and freeze them
    at its fair share, directed per-link capacity (full duplex)."""
    active = set(flow_links)
    rate = {f: 0.0 for f in flow_links}
    link_usage = {}
    for f, links in flow_links.items():
        for link in links:
            link_usage.setdefault(link, set()).add(f)
    remaining_capacity = {link: capacity for link in link_usage}

    while active:
        best_share, best_link = demand, None
        for link, flows in link_usage.items():
            on = flows & active
            if not on:
                continue
            share = remaining_capacity[link] / len(on)
            if share < best_share:
                best_share, best_link = share, link

        finishing = (link_usage[best_link] & active) if best_link is not None else set(active)
        for f in finishing:
            rate[f] = best_share
            active.discard(f)
            for link in flow_links[f]:
                remaining_capacity[link] -= best_share

    return rate


def test_e2_collision_ceiling_k8_independent_reimplementation():
    """k=8, full random-permutation traffic, 0% failures: this independent
    path should land near the ~47% (+/- 2%) PHASE4.md Step 5 expects --
    confirming the main analysis.py e2-throughput pipeline (which reports
    47.4% +/- 1.0 over 20 trials, seed=1) isn't a self-consistent bug."""
    k = 8
    g = build_fat_tree(k)
    hosts = hosts_of(g)
    choice_rng = random.Random(1)

    pcts = []
    for trial in range(15):
        pairs = _independent_random_permutation(hosts, random.Random(1000 + trial))
        flow_links = {}
        for idx, (src, dst) in enumerate(pairs):
            paths = list(nx.all_shortest_paths(g, src, dst))
            path = choice_rng.choice(paths)
            flow_links[idx] = list(zip(path, path[1:]))
        rates = _independent_max_min_fair(flow_links)
        achieved = sum(rates.values())
        ideal = len(pairs) * 1.0
        pcts.append(100 * achieved / ideal)

    mean_pct = sum(pcts) / len(pcts)
    assert mean_pct == pytest.approx(47, abs=3)
