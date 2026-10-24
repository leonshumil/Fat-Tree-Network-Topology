"""Routing scheme registry: get_router(name, g, cfg) -> Router.

Selected by config `routing: single-path | ecmp | two-level | classifier | scheduler`.
"""
from fattree.routing.base import Router
from fattree.routing.ecmp import EcmpRouter
from fattree.routing.flow_classifier import FlowClassifierRouter
from fattree.routing.flow_scheduler import FlowSchedulerRouter
from fattree.routing.single_path import SinglePathRouter
from fattree.routing.two_level import TwoLevelRouter

_REGISTRY = {
    "single-path": SinglePathRouter,
    "ecmp": EcmpRouter,
    "two-level": TwoLevelRouter,
    "classifier": FlowClassifierRouter,
    "scheduler": FlowSchedulerRouter,
}


def get_router(name, g, cfg):
    try:
        cls = _REGISTRY[name]
    except KeyError:
        raise ValueError(f"unknown routing scheme: {name!r}, must be one of {sorted(_REGISTRY)}")
    return cls(g, cfg)
