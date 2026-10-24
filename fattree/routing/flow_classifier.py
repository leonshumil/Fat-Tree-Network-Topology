"""Flow classification with dynamic port reassignment (paper Sec. 3.6 + Sec. 4.2,
Algorithm 3): at each pod switch, a NEW flow is assigned to the currently
least-loaded upward port (instead of two-level's static host-ID formula); every
`rearrange_interval` (paper: t = 1s; here, one `route_all()` batch = one interval)
up to `classifier.max_moves` flows are moved from the most- to the least-loaded
port at that switch if doing so reduces the imbalance, repeated for up to
`classifier.max_rounds` or until a round makes no move ("stable").

Downward (same-pod) and core-to-destination-pod hops are unambiguous and reuse
two-level's tables unchanged -- only the two UPWARD hops (edge->agg, agg->core)
are classifier-controlled.

This scheme is inherently batch (rearrangement needs the whole flow set), so
`route_all(flows)` is the primary entry point (used by the flow-level simulator,
Step 4). `route(flow)` is a thin single-flow wrapper for Router-interface
compatibility; with one flow there is nothing to rearrange.
"""
from fattree.routing.base import Router
from fattree.routing.two_level import build_pod_switch_table
from fattree.topology import core_switch_name, pod_switch_name


class FlowClassifierRouter(Router):
    def __init__(self, g, cfg):
        super().__init__(g, cfg)
        self.k = cfg["topology"]["k"]
        self.half = self.k // 2
        classifier_cfg = cfg.get("classifier", {})
        self.max_moves = classifier_cfg.get("max_moves", 3)
        self.max_rounds = classifier_cfg.get("max_rounds", 20)
        self._pod_tables = {z: build_pod_switch_table(z, self.half) for z in range(self.k)}
        self._failed_links = set()
        self._failed_switches = set()
        self._last_rearrangements = 0

    def on_failure(self, failed_links, failed_switches):
        self._failed_links = {frozenset(e) for e in failed_links}
        self._failed_switches = set(failed_switches)

    def _link_alive(self, u, v):
        if u in self._failed_switches or v in self._failed_switches:
            return False
        return frozenset((u, v)) not in self._failed_links

    def _upward_neighbors(self, node, d):
        if d["type"] == "edge":
            return [pod_switch_name(d["pod"], s, self.half) for s in range(self.half, self.k)]
        row_j = (d["switch"] - self.half) + 1
        return [core_switch_name(row_j, i) for i in range(1, self.half + 1)]

    def _collect_upward_choice(self, flow, load):
        """Walk from flow's first-hop edge switch, greedily picking the
        least-loaded live upward port at each switch that needs one. Returns
        {node: chosen_next_hop} (possibly empty, if no upward hop is needed), or
        None if the flow cannot even leave its source edge switch."""
        dst = self.g.nodes[flow.dst]
        dst_pod, dst_switch = dst["pod"], dst["switch"]
        first_hop = pod_switch_name(self.g.nodes[flow.src]["pod"], self.g.nodes[flow.src]["switch"], self.half)
        if not self._link_alive(flow.src, first_hop):
            return None

        choices = {}
        node = first_hop
        while True:
            d = self.g.nodes[node]
            if d["type"] == "edge" and d["pod"] == dst_pod and d["switch"] == dst_switch:
                return choices  # direct delivery, no upward hop from here
            if d["type"] == "agg" and d["pod"] == dst_pod:
                return choices  # deterministic downward hop, handled in _build_path

            candidates = [n for n in self._upward_neighbors(node, d) if self._link_alive(node, n)]
            if not candidates:
                return None
            chosen = min(candidates, key=lambda n: load.get((node, n), 0))
            load[(node, chosen)] = load.get((node, chosen), 0) + flow.size_bytes
            choices[node] = chosen
            was_edge = d["type"] == "edge"
            node = chosen
            if not was_edge:
                return choices  # just made the single agg->core choice; rest is deterministic

    def _rearrange(self, flows, assignment, load):
        self._last_rearrangements = 0
        for _round in range(self.max_rounds):
            moved_any = False
            neighbors_by_node = {}
            for choices in assignment.values():
                if not choices:
                    continue
                for node, neighbor in choices.items():
                    neighbors_by_node.setdefault(node, set()).add(neighbor)

            for node, neighbors in neighbors_by_node.items():
                loads = {n: load.get((node, n), 0) for n in neighbors}
                if len(loads) < 2:
                    continue
                busiest = max(loads, key=loads.get)
                idlest = min(loads, key=loads.get)
                if loads[busiest] <= loads[idlest]:
                    continue
                movable = [idx for idx, choices in assignment.items() if choices and choices.get(node) == busiest]
                for idx in movable[: self.max_moves]:
                    size = flows[idx].size_bytes
                    if loads[busiest] - size < loads[idlest] + size:
                        break  # would overshoot past idlest -- stop moving this pair
                    assignment[idx][node] = idlest
                    load[(node, busiest)] -= size
                    load[(node, idlest)] = load.get((node, idlest), 0) + size
                    loads[busiest] -= size
                    loads[idlest] += size
                    moved_any = True
                    self._last_rearrangements += 1
            if not moved_any:
                break

    def _build_path(self, flow, choices):
        dst = self.g.nodes[flow.dst]
        dst_pod, dst_switch = dst["pod"], dst["switch"]
        first_hop = pod_switch_name(self.g.nodes[flow.src]["pod"], self.g.nodes[flow.src]["switch"], self.half)
        if not self._link_alive(flow.src, first_hop):
            return None

        path = [flow.src, first_hop]
        node = first_hop
        while node != flow.dst:
            d = self.g.nodes[node]
            if d["type"] == "edge" and d["pod"] == dst_pod and d["switch"] == dst_switch:
                nxt = flow.dst
            elif d["type"] == "agg" and d["pod"] == dst_pod:
                port = self._pod_tables[d["switch"]]["prefix"][dst_switch]
                nxt = pod_switch_name(d["pod"], port, self.half)
            elif node in choices:
                nxt = choices[node]
            elif d["type"] == "core":
                nxt = pod_switch_name(dst_pod, self.half + (d["j"] - 1), self.half)
            else:
                return None
            if not self._link_alive(node, nxt):
                return None
            path.append(nxt)
            node = nxt
        return path

    def route_all(self, flows):
        """Assign every flow a path: greedy least-loaded placement, then
        rearrangement rounds. Returns list[path or None], aligned with `flows`."""
        load = {}
        assignment = {idx: self._collect_upward_choice(flow, load) for idx, flow in enumerate(flows)}
        self._rearrange(flows, assignment, load)
        return [
            self._build_path(flow, assignment[idx]) if assignment[idx] is not None else None
            for idx, flow in enumerate(flows)
        ]

    def route(self, flow):
        return self.route_all([flow])[0]

    def stats(self):
        return {"rearrangements": self._last_rearrangements}
