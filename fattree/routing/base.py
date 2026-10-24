"""Router interface shared by every routing scheme in fattree/routing/."""


class Router:
    def __init__(self, g, cfg):
        self.g = g
        self.cfg = cfg

    def route(self, flow):
        """Return list[node] from flow.src to flow.dst (inclusive), or None if the
        flow cannot currently be delivered (e.g. a table-driven hop crosses a
        failed link and the scheme does not reroute around it)."""
        raise NotImplementedError

    def on_failure(self, failed_links, failed_switches):
        """React to a failure event. `failed_links` is a set of (u, v) tuples,
        `failed_switches` a set of node names. Default: no-op -- stateless schemes
        (ecmp) just recompute live over the current graph on every route() call."""
        pass

    def stats(self):
        """Scheme-specific stats (e.g. scheduler placement time/memory, number of
        flow rearrangements). Default: empty."""
        return {}
