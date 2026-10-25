"""ECMP routing: hash a flow onto one of the equal-cost shortest paths currently
available between its source and destination (industry standard, CLAUDE.md
"ECMP"). Unlike two-level's static tables, this recomputes over the live
(post-failure) graph on every call, so it naturally reroutes around a failure
whenever another shortest-length path exists -- no on_failure() hook needed.

Simplification: the paper hashes a packet's 5-tuple independently at every hop;
we approximate per-flow path stability by hashing the whole flow once (a
deterministic hash, not Python's randomized str hash) and using it to pick among
all equal-cost shortest paths, so the same flow always takes the same path while
different flows between the same host pair spread across the equal-cost paths.

Path enumeration uses scipy's sparse (C-implemented) single-source BFS for
distances -- cached per source node for this router instance's lifetime --
then reconstructs every shortest path in plain Python by walking backward
from the destination, at each step only stepping to a neighbor exactly one
hop closer to the source (the same result nx.all_shortest_paths gives, just
without its much slower pure-Python predecessor BFS underneath; this is the
same trick fattree/sim/graph.py already uses for the same reason). Profiling
a k=24 random-permutation flow-level run (Phase 4, PHASE4.md Step 5) showed
the old nx.all_shortest_paths call alone was ~75% of total runtime -- it
reran a full single-source BFS from scratch for every flow, even though a
BFS from one source is identical work regardless of which flow asked for it.
"""
import hashlib

import numpy as np
import scipy.sparse as sp
from scipy.sparse.csgraph import shortest_path as _sp_shortest_path

from fattree.routing.base import Router


def _flow_hash(flow):
    key = f"{flow.src}|{flow.dst}|{flow.size_bytes}|{flow.start_time}|{flow.cls}".encode()
    return int(hashlib.md5(key).hexdigest(), 16)


class EcmpRouter(Router):
    def __init__(self, g, cfg):
        super().__init__(g, cfg)
        self._nodes = list(self.g.nodes())
        self._index = {n: i for i, n in enumerate(self._nodes)}
        n = len(self._nodes)
        rows, cols = [], []
        for u, v in self.g.edges():
            iu, iv = self._index[u], self._index[v]
            rows.append(iu); cols.append(iv)
            rows.append(iv); cols.append(iu)
        data = np.ones(len(rows)) if rows else np.array([])
        self._adj = sp.csr_matrix((data, (rows, cols)), shape=(n, n))
        self._dist_cache = {}  # src node -> distance array, reused across route() calls

    def _distances_from(self, src):
        dist = self._dist_cache.get(src)
        if dist is None:
            src_idx = self._index[src]
            dist = _sp_shortest_path(self._adj, method="D", unweighted=True, directed=False, indices=[src_idx])[0]
            self._dist_cache[src] = dist
        return dist

    def _all_shortest_paths(self, src, dst):
        if src not in self._index or dst not in self._index:
            return []
        if src == dst:
            return [[src]]
        dist = self._distances_from(src)
        dst_dist = dist[self._index[dst]]
        if not np.isfinite(dst_dist):
            return []

        paths = [[dst]]
        remaining = int(dst_dist)
        while remaining > 0:
            next_paths = []
            for path in paths:
                last = path[-1]
                for nb in self.g.neighbors(last):
                    if dist[self._index[nb]] == remaining - 1:
                        next_paths.append(path + [nb])
            paths = next_paths
            remaining -= 1
        return [list(reversed(p)) for p in paths]

    def route(self, flow):
        paths = self._all_shortest_paths(flow.src, flow.dst)
        if not paths:
            return None
        return paths[_flow_hash(flow) % len(paths)]
