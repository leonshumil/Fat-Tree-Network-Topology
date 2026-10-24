"""Single-path routing: one fixed shortest path per (src, dst), computed once
against the initial (pre-failure) topology and never recomputed -- an OSPF-like
baseline with no multi-path diversity and no rerouting on failure (CLAUDE.md:
"single shortest path")."""
import networkx as nx

from fattree.routing.base import Router


class SinglePathRouter(Router):
    def __init__(self, g, cfg):
        super().__init__(g, cfg)
        self._base_graph = g.copy()
        self._cache = {}
        self._failed_links = set()
        self._failed_switches = set()

    def on_failure(self, failed_links, failed_switches):
        self._failed_links = {frozenset(e) for e in failed_links}
        self._failed_switches = set(failed_switches)

    def route(self, flow):
        key = (flow.src, flow.dst)
        if key not in self._cache:
            try:
                self._cache[key] = nx.shortest_path(self._base_graph, flow.src, flow.dst)
            except nx.NetworkXNoPath:
                self._cache[key] = None
        path = self._cache[key]
        if path is None:
            return None
        for u, v in zip(path, path[1:]):
            if u in self._failed_switches or v in self._failed_switches:
                return None
            if frozenset((u, v)) in self._failed_links:
                return None
        return path
