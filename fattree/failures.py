"""Link and switch failure injection.

Two selection modes (config key `mode`):
  exact      - round(pct/100 * N) elements chosen uniformly without replacement.
  bernoulli  - each eligible element fails independently with probability p = pct/100.

`link_pct` / `switch_pct` may each be a fixed number, or a `[min, max]` range in
config/CLI, in which case the actual percentage is drawn uniformly at random from
that range using the run's seeded RNG (so it stays reproducible for a given seed).

Switch failures remove the node and all its incident links; link failures are
applied afterward so switch_pct and link_pct compose (a link cannot fail twice).
"""
import random


def resolve_pct(value, rng):
    """Return a concrete percentage: pass scalars through, draw [min, max] ranges."""
    if isinstance(value, (list, tuple)):
        lo, hi = value
        return rng.uniform(lo, hi)
    return value


def _select(eligible, pct, mode, rng):
    if mode == "exact":
        n_fail = round(pct / 100 * len(eligible))
        return set(rng.sample(eligible, n_fail)) if n_fail else set()
    if mode == "bernoulli":
        p = pct / 100
        return {e for e in eligible if rng.random() < p}
    raise ValueError(f"unknown failure mode: {mode!r}")


def select_failed_links(g, layers, pct, mode, rng):
    eligible = [(u, v) for u, v, d in g.edges(data=True) if d["layer"] in layers]
    return _select(eligible, pct, mode, rng)


def select_failed_switches(g, layers, pct, mode, rng):
    eligible = [n for n, d in g.nodes(data=True) if d.get("type") in layers]
    return _select(eligible, pct, mode, rng)


def apply_failures(g, failures_cfg, seed):
    """Return (g_after_failures, record).

    record has 'failed_switches'/'failed_links' (the removed elements) and the
    resolved 'switch_pct'/'link_pct' actually used (useful when either was given
    as a [min, max] range).

    `failures_cfg` is the `failures` section of the config (link_pct, switch_pct,
    link_layers, switch_layers, mode). Does not mutate `g`.
    """
    rng = random.Random(seed)
    g2 = g.copy()

    switch_pct = resolve_pct(failures_cfg["switch_pct"], rng)
    failed_switches = select_failed_switches(
        g2, failures_cfg["switch_layers"], switch_pct, failures_cfg["mode"], rng
    )
    g2.remove_nodes_from(failed_switches)

    link_pct = resolve_pct(failures_cfg["link_pct"], rng)
    failed_links = select_failed_links(
        g2, failures_cfg["link_layers"], link_pct, failures_cfg["mode"], rng
    )
    g2.remove_edges_from(failed_links)

    return g2, {
        "failed_switches": failed_switches,
        "failed_links": failed_links,
        "switch_pct": switch_pct,
        "link_pct": link_pct,
    }
