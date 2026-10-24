"""Two-level routing (paper Sec. 3.3-3.5, Algorithms 1 and 2).

Builds the paper's two-level prefix/suffix forwarding tables (not a shortest-path
shortcut): every pod switch gets a prefix table for directly-reachable subnets
(empty for edge switches -- Sec. 3.5: "For the lower pod switches, we simply omit
the subnet prefix step") plus a suffix table keyed by destination host ID
(Algorithm 1's `(i - 2 + z) mod (k/2) + k/2`, which spreads traffic with the same
host ID arriving from different lower switches across different upper ports);
every core switch gets one terminating /16 entry per destination pod (Algorithm 2
-- the paper's pseudocode loops destination pods 0..k/2-1, a documented typo; this
loops the full 0..k-1, per CLAUDE.md Sec. 5).

Resolving a table port number to an actual next-hop node uses the paper's wiring:
  - edge -> agg:  the port value IS the target agg switch's global `switch` index
                  (both range over [k/2, k-1]).
  - agg  -> core: the raw suffix-table port (range [k/2, k-1]) maps to the core's
                  row-local index via i = k - port. This is the one place a direct
                  identity map is wrong -- verified against the paper's own worked
                  example (Sec. 3.5): agg switch 10.0.2.1 (k=4) computes port 3 for
                  destination host ID 3 and forwards to core switch 10.4.1.1 (i=1,
                  not i=2). See test_two_level_matches_paper_fig3_example.
  - core -> agg:  the destination pod's agg switch is the core switch's only
                  neighbor in that pod (bijective by construction; no table needed).
  - agg  -> edge: the port value IS the target edge switch's global `switch` index
                  (both range over [0, k/2-1]) -- the destination subnet's own switch.

Failures: by default (config `two_level.failover: false`) these tables are static
and do NOT react to failures -- a flow whose table-driven hop crosses a failed
link/switch is simply dropped, reproducing the paper's documented weakness
(Sec. 3.1: "small subset of core switches... chosen... congestion";  Sec. 3.8
motivates failover for exactly this reason). With `two_level.failover: true`, a
dead primary hop is replaced by another live port at the same switch (an upward
port at the same switch for edge/agg, approximating the paper's Sec. 3.8
broadcast-based avoidance "when a choice exists") instead of dropping the flow.
"""
from fattree.routing.base import Router
from fattree.topology import core_switch_name, pod_switch_name


def edge_suffix_port(host_id, z, half):
    """Algorithm 1's suffix formula: upward port at pod switch z for host ID i."""
    return (host_id - 2 + z) % half + half


def build_pod_switch_table(z, half):
    """Algorithm 1 for one pod switch of local index z. Edge switches (z < half)
    get no prefix table (Sec. 3.5); agg switches (z >= half) get one /24 prefix
    entry per edge switch in the pod (port = that edge switch's index)."""
    suffix = {host_id: edge_suffix_port(host_id, z, half) for host_id in range(2, half + 2)}
    prefix = {} if z < half else {i: i for i in range(half)}
    return {"prefix": prefix, "suffix": suffix}


def build_core_table(k):
    """Algorithm 2, typo fixed: one /16 prefix per pod, port = pod index directly."""
    return {dst_pod: dst_pod for dst_pod in range(k)}


class TwoLevelRouter(Router):
    def __init__(self, g, cfg):
        super().__init__(g, cfg)
        self.k = cfg["topology"]["k"]
        self.half = self.k // 2
        self.failover = cfg.get("two_level", {}).get("failover", False)
        self._pod_tables = {z: build_pod_switch_table(z, self.half) for z in range(self.k)}
        self._core_table = build_core_table(self.k)
        self._failed_links = set()
        self._failed_switches = set()

    def on_failure(self, failed_links, failed_switches):
        self._failed_links = {frozenset(e) for e in failed_links}
        self._failed_switches = set(failed_switches)

    def _link_alive(self, u, v):
        if u in self._failed_switches or v in self._failed_switches:
            return False
        return frozenset((u, v)) not in self._failed_links

    def _primary_next_hop(self, node, d, dst_pod, dst_switch, dst_id):
        """The table-driven next hop with no failure awareness."""
        if d["type"] == "edge":
            if d["pod"] == dst_pod and d["switch"] == dst_switch:
                return None  # destination host is directly attached -- caller delivers
            port = self._pod_tables[d["switch"]]["suffix"][dst_id]
            return pod_switch_name(d["pod"], port, self.half)
        if d["type"] == "agg":
            if d["pod"] == dst_pod:
                port = self._pod_tables[d["switch"]]["prefix"][dst_switch]
                return pod_switch_name(d["pod"], port, self.half)
            port = self._pod_tables[d["switch"]]["suffix"][dst_id]
            row_j = (d["switch"] - self.half) + 1
            core_i = self.k - port
            return core_switch_name(row_j, core_i)
        if d["type"] == "core":
            port = self._core_table[dst_pod]
            row_j, _i = d["j"], d["i"]
            return pod_switch_name(port, self.half + (row_j - 1), self.half)
        raise ValueError(f"two-level routing reached an unexpected node type: {d['type']!r}")

    def _alternate_upward_ports(self, node, d):
        """Other live upward ports at this switch, for failover (Sec. 3.8)."""
        if d["type"] == "edge":
            return [pod_switch_name(d["pod"], s, self.half) for s in range(self.half, self.k)]
        if d["type"] == "agg":
            row_j = (d["switch"] - self.half) + 1
            return [core_switch_name(row_j, i) for i in range(1, self.half + 1)]
        return []

    def route(self, flow):
        dst = self.g.nodes[flow.dst]
        dst_pod, dst_switch, dst_id = dst["pod"], dst["switch"], dst["host_id"]

        first_hop = pod_switch_name(self.g.nodes[flow.src]["pod"], self.g.nodes[flow.src]["switch"], self.half)
        if not self._link_alive(flow.src, first_hop):
            return None

        path = [flow.src, first_hop]
        node = first_hop
        seen = {first_hop}
        while node != flow.dst:
            d = self.g.nodes[node]
            nxt = self._primary_next_hop(node, d, dst_pod, dst_switch, dst_id)
            if nxt is None:
                nxt = flow.dst

            if not self._link_alive(node, nxt) and nxt != flow.dst:
                if not self.failover:
                    return None
                alive_alt = next(
                    (alt for alt in self._alternate_upward_ports(node, d)
                     if alt != nxt and alt not in seen and self._link_alive(node, alt)),
                    None,
                )
                if alive_alt is None:
                    return None
                nxt = alive_alt
            elif not self._link_alive(node, nxt):
                return None  # last hop to the destination host itself is down

            path.append(nxt)
            seen.add(nxt)
            node = nxt
        return path
