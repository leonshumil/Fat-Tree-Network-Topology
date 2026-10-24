"""Central flow scheduler (paper Sec. 3.7 + Sec. 4.3): edge switches report flows
at or above `traffic.elephant_threshold_bytes` to a central scheduler, which keeps
global link-reservation state and linearly searches the (k/2)^2 inter-pod core
paths (or k/2 intra-pod agg paths) for one whose links are all unreserved and
unfailed, reserving it for that elephant. Mouse flows fall back to two-level
routing (Sec. 3.7). Records wall-clock time per placement and the size of its
link-state/flow-state tables, to reproduce the paper's Table 3
(PHASE2_PROMPT.md experiment `scheduler_cost_vs_k`).
"""
import time

from fattree.routing.base import Router
from fattree.routing.two_level import TwoLevelRouter
from fattree.topology import core_switch_name, pod_switch_name


class FlowSchedulerRouter(Router):
    def __init__(self, g, cfg):
        super().__init__(g, cfg)
        self.k = cfg["topology"]["k"]
        self.half = self.k // 2
        self.elephant_threshold = cfg["traffic"].get("elephant_threshold_bytes", 10_000_000)
        self._two_level = TwoLevelRouter(g, cfg)
        self._reserved_links = set()     # link-state: frozenset({u, v}) currently reserved
        self._flow_reservations = {}     # flow-state: flow -> [reserved link keys]
        self._failed_links = set()
        self._failed_switches = set()
        self._placement_times = []

    def on_failure(self, failed_links, failed_switches):
        self._failed_links = {frozenset(e) for e in failed_links}
        self._failed_switches = set(failed_switches)
        self._two_level.on_failure(failed_links, failed_switches)

    def _link_usable(self, u, v):
        if u in self._failed_switches or v in self._failed_switches:
            return False
        key = frozenset((u, v))
        return key not in self._failed_links and key not in self._reserved_links

    def _candidate_core_paths(self, src_edge, src_pod, dst_edge, dst_pod):
        """All (k/2)^2 inter-pod core paths: src_edge -> agg -> core -> agg -> dst_edge."""
        for a_local in range(self.half):
            src_agg = pod_switch_name(src_pod, self.half + a_local, self.half)
            dst_agg = pod_switch_name(dst_pod, self.half + a_local, self.half)
            row_j = a_local + 1
            for i in range(1, self.half + 1):
                yield [src_edge, src_agg, core_switch_name(row_j, i), dst_agg, dst_edge]

    def _candidate_intra_pod_paths(self, src_edge, dst_edge, pod):
        """All k/2 intra-pod agg paths: src_edge -> agg -> dst_edge."""
        for a_local in range(self.half):
            yield [src_edge, pod_switch_name(pod, self.half + a_local, self.half), dst_edge]

    def _reserve(self, path):
        links = [frozenset((u, v)) for u, v in zip(path, path[1:])]
        self._reserved_links.update(links)
        return links

    def route(self, flow):
        if flow.size_bytes < self.elephant_threshold:
            return self._two_level.route(flow)  # mice: two-level (Sec. 3.7)

        start = time.perf_counter()
        src, dst = self.g.nodes[flow.src], self.g.nodes[flow.dst]
        src_edge = pod_switch_name(src["pod"], src["switch"], self.half)
        dst_edge = pod_switch_name(dst["pod"], dst["switch"], self.half)

        if not self._link_usable(flow.src, src_edge) or not self._link_usable(dst_edge, flow.dst):
            self._placement_times.append(time.perf_counter() - start)
            return None

        if src["pod"] == dst["pod"]:
            candidates = self._candidate_intra_pod_paths(src_edge, dst_edge, src["pod"])
        else:
            candidates = self._candidate_core_paths(src_edge, src["pod"], dst_edge, dst["pod"])

        chosen = None
        for middle in candidates:
            hops = [flow.src] + middle + [flow.dst]
            if all(self._link_usable(u, v) for u, v in zip(hops, hops[1:])):
                chosen = hops
                break

        self._placement_times.append(time.perf_counter() - start)
        if chosen is None:
            return None
        self._flow_reservations[flow] = self._reserve(chosen)
        return chosen

    def release(self, flow):
        """Free a completed elephant flow's reserved links (caller's responsibility
        once the flow finishes, e.g. the flow-level simulator)."""
        links = self._flow_reservations.pop(flow, None)
        if links:
            for link in links:
                self._reserved_links.discard(link)

    def stats(self):
        n = len(self._placement_times)
        mean_time = sum(self._placement_times) / n if n else 0.0
        return {
            "placements": n,
            "mean_placement_time_s": mean_time,
            "link_state_entries": len(self._reserved_links),
            "flow_state_entries": len(self._flow_reservations),
        }
