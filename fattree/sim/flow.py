"""Flow-level simulation (PHASE2_PROMPT.md Step 4): given a topology, a routed
set of flows, and link capacities, compute per-flow throughput via max-min fair
rate allocation (progressive filling / water-filling). Queueing, packetization,
and retransmission are NOT modeled -- that is the packet level.

Outputs: per-flow rate, link utilization, and aggregate throughput as a % of
ideal, where ideal = sum of host send rates (paper Sec. 5's bisection-bandwidth
baseline; CLAUDE.md Sec. 5 notes this is the sum of all hosts' offered rates --
e.g. 16 x 96 Mb/s for the paper's k=4 example -- not N/2).
"""
import heapq
import itertools


def max_min_fair_rates(flow_links, demands, link_capacity):
    """Progressive filling / water-filling max-min fair allocation.

    `flow_links`: {flow_id: [link_key, ...]}, the links each flow's path uses.
    `demands`: {flow_id: max_rate}, an upper bound on that flow's own rate (e.g.
    its source host's offered load) -- modeled as a private "virtual link" so the
    same algorithm handles both shared-link contention and per-flow caps.
    `link_capacity`: a scalar (same for every real link) or {link_key: capacity}.

    Returns {flow_id: allocated_rate}.

    Uses a lazy-deletion min-heap of (share, link) to find the next bottleneck
    link, instead of rescanning every link on every iteration: at large k, most
    iterations finalize only one or two flows out of thousands still active
    (profiled on a k=32 random-permutation run, Phase 4 PHASE4.md Step 5), so a
    full linear rescan of every link -- including the thousands already fully
    resolved -- dominated runtime (~50s -> <1s after this fix). A link's heap
    entries go stale whenever its remaining capacity or active-flow count
    changes; a popped entry is checked against freshly recomputed values and
    discarded if it no longer matches (the up-to-date entry for that link is
    already elsewhere in the heap, pushed when the change happened).
    """
    active = set(flow_links)
    rate = {f: 0.0 for f in flow_links}
    link_flows = {}
    remaining = {}
    flow_all_links = {}

    for f, links in flow_links.items():
        demand_link = ("_demand", f)
        all_links = list(links) + [demand_link]
        flow_all_links[f] = all_links
        for link in all_links:
            link_flows.setdefault(link, set()).add(f)
            if link not in remaining:
                if link == demand_link:
                    remaining[link] = demands.get(f, float("inf"))
                else:
                    remaining[link] = link_capacity if isinstance(link_capacity, (int, float)) else link_capacity[link]

    active_count = {link: len(flows) for link, flows in link_flows.items()}
    counter = itertools.count()
    heap = []

    def push(link):
        n = active_count[link]
        if n > 0:
            heapq.heappush(heap, (remaining[link] / n, next(counter), link))

    for link in link_flows:
        push(link)

    while active and heap:
        share, _, link = heapq.heappop(heap)
        n = active_count[link]
        if n <= 0 or remaining[link] / n != share:
            continue  # stale: a fresher entry for this link exists (or did) in the heap

        removed = link_flows[link] & active
        if not removed:
            continue
        for f in removed:
            rate[f] = share
            active.discard(f)
            for l in flow_all_links[f]:
                remaining[l] -= share
                active_count[l] -= 1
                push(l)

    return rate


def simulate_flows(router, flows, link_capacity_gbps, load):
    """Route every flow, then allocate max-min fair rates over the resulting
    paths. Flows the router cannot deliver (route() returns None) are dropped --
    they still count toward `ideal_total_gbps` (they wanted to send) but
    contribute 0 to `achieved_total_gbps`.
    """
    flow_links = {}
    dropped = []
    for idx, flow in enumerate(flows):
        path = router.route(flow)
        if path is None:
            dropped.append(idx)
            continue
        # Directed (u, v) tuples, NOT frozenset(u, v): real links are full-duplex
        # with independent capacity per direction. Treating a link as undirected
        # here made unrelated opposite-direction flows (e.g. a chain pattern
        # where host B both receives from A and sends to C through the same
        # physical link) falsely contend for one shared capacity, capping
        # throughput at ~50% across many patterns -- caught by the paper's own
        # Table 2 reproduction (analysis.py table2) showing an implausible flat
        # 50% ceiling on almost every pattern/scheme combination.
        flow_links[idx] = list(zip(path, path[1:]))

    demands = {idx: load * link_capacity_gbps for idx in flow_links}
    rates = max_min_fair_rates(flow_links, demands, link_capacity_gbps)

    link_load = {}
    for idx, links in flow_links.items():
        for link in links:
            link_load[link] = link_load.get(link, 0.0) + rates[idx]
    link_utilization = {link: amount / link_capacity_gbps for link, amount in link_load.items()}

    ideal_total = load * link_capacity_gbps * len(flows)
    achieved_total = sum(rates.values())

    return {
        "rates": rates,
        "dropped": dropped,
        "link_utilization": link_utilization,
        "max_link_load": max(link_utilization.values()) if link_utilization else 0.0,
        "achieved_total_gbps": achieved_total,
        "ideal_total_gbps": ideal_total,
        "throughput_pct_of_ideal": (achieved_total / ideal_total * 100) if ideal_total else 0.0,
    }
