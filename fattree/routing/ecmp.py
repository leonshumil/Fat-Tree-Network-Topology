"""ECMP routing: hash a flow onto one of the equal-cost shortest paths currently
available between its source and destination (industry standard, CLAUDE.md
"ECMP"). Unlike two-level's static tables, this recomputes over the live
(post-failure) graph on every call, so it naturally reroutes around a failure
whenever another shortest-length path exists -- no on_failure() hook needed.

Simplification: the paper hashes a packet's 5-tuple independently at every hop;
we approximate per-flow path stability by hashing the whole flow once (a
deterministic hash, not Python's randomized str hash) and using it to pick among
nx.all_shortest_paths(), so the same flow always takes the same path while
different flows between the same host pair spread across the equal-cost paths.
"""
import hashlib

import networkx as nx

from fattree.routing.base import Router


def _flow_hash(flow):
    key = f"{flow.src}|{flow.dst}|{flow.size_bytes}|{flow.start_time}|{flow.cls}".encode()
    return int(hashlib.md5(key).hexdigest(), 16)


class EcmpRouter(Router):
    def route(self, flow):
        try:
            paths = list(nx.all_shortest_paths(self.g, flow.src, flow.dst))
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None
        if not paths:
            return None
        return paths[_flow_hash(flow) % len(paths)]
