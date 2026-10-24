"""A Router that replays paths decided ahead of time, keyed by flow identity.

Used to drive the packet-level simulator (which calls `router.route(flow)`
once per flow, at send time) with schemes whose real interface is a *batch*
decision over many flows at once -- `FlowClassifierRouter.route_all()` and
`FlowSchedulerRouter`'s per-flow reservation search, called here up front over
the whole arrival batch rather than continuously inside the packet engine.
See PHASE3_3_EXP7_MICE_ELEPHANTS.md's README discussion for why (a documented
simplification: routing decisions are made once per flow at arrival time using
the already-known batch, not truly continuously adaptive mid-flow).
"""
from fattree.routing.base import Router


class PrecomputedPathRouter(Router):
    def __init__(self, paths_by_flow_id, stats=None):
        self._paths = paths_by_flow_id  # {id(flow): path or None}
        self._stats = stats or {}

    def route(self, flow):
        return self._paths.get(id(flow))

    def stats(self):
        return self._stats
