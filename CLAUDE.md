# Fat-Tree Network Topology – Homework 1

Course 361.2.2460, Data center & AI networking (BGU, semester A 2026/27).
Paper [1]: M. Al-Fares, A. Loukissas, A. Vahdat, "A Scalable, Commodity Data Center Network
Architecture", SIGCOMM 2008, doi:10.1145/1402946.1402967.

This file is the project spec. Read it fully before changing code.

---

## 1. Assignment requirements (from the homework PDF)
1. Submit a GitHub repo: full source + README.md (approach, how to run, results).
2. Topology: three-tier fat-tree consistent with the paper.
3. Define and document input parameters; code must handle different scales.
4. Optional CLI parameter or config file for % link failures.
5. Quantitative analysis of a non-trivial metric as a function of one input parameter;
   visualize with plots. Homework examples: avg path length / number of paths vs failure
   probability (advanced); switches with multiple failed links at 1% vs port count
   (intermediate); hosts vs port count (basic).
6. README: for every plot, an example with (a) setup, experiment and parameters,
   (b) discussion of results, explanation and significance.
- AI usage is allowed but must be referenced in the README. The student must understand
  and be able to present every line.

Deadlines:
- **22/10/26 08:00**: GitHub link with initial code + README.md (20%)
- **25/10/26 12:00**: full homework (80%)
- Results are presented in class on 22/10, 25/10 and 1/11.

## 2. Goals for this repo (beyond the minimum)
- Python simulation core with a YAML config (k, link failure %, switch failure %, load,
  traffic pattern, routing, trials, seed, sweeps).
- Analysis that extracts the homework metrics plus the extra ones listed in §6.
- Visualization: charts, pies, heatmaps (matplotlib) for the README.
- A GitHub Pages site (`web/`) with an interactive, visually appealing topology:
  SVG icons for servers and switches, labels (paper IP addresses), failed links/switches
  marked, link load colored, plus the result charts.
- Not using Click (the paper's packet-level emulator). This project is a **flow-level graph
  simulation**; state that in the README.

## 3. Repository layout

**Status key:** ✅ built | ⬜ not built (see README.md "Not done / future work")

```
Fat-Tree-Network-Topology/
├── config/
│   ├── default.yaml            # one scenario                                  ✅
│   └── experiments/*.yaml      # not used; analysis.py subcommands take CLI    ⬜
│                                 flags directly instead (pragmatic deviation,
│                                 see README.md Install & run)
├── fattree/                    # simulation core (Python package)
│   ├── topology.py             # build k-ary fat-tree + traditional tree,      ✅
│   │                             paper addressing, validate()
│   ├── failures.py             # link + switch failures, per layer, exact/     ✅
│   │                             bernoulli, fixed or randomized [min,max] pct
│   ├── traffic.py              # 8 traffic patterns (Sec. 5.2) + flow size/    ✅
│   │                             class models
│   ├── routing/                # single_path, ecmp, two_level (Alg. 1/2),     ✅
│   │                             flow_classifier (Alg. 3), flow_scheduler
│   │                             (package, not routing.py)
│   ├── metrics.py              # switches_with_multiple_failed_links only;    partial
│   │                             rest of §6 covered by fattree/sim/graph.py
│   ├── sim/                    # graph.py, flow.py, packet.py (package, not   ✅
│   │                             simulate.py -- one engine per file per
│   │                             PHASE2_PROMPT.md Step 4)
│   └── export.py               # topology/results -> JSON for the web page   ⬜
├── run.py                      # CLI: build a topology, apply failures,       ✅
│                                 print a summary (not yet wired to traffic/
│                                 routing/sim -- those are driven by analysis.py)
├── analysis.py                 # CLI subcommands, one per experiment, each    ✅ (4/9)
│                                 aggregating trials -> results/*.csv +
│                                 figures/*.png (see README.md Results)
├── visualize.py                # topology diagram, failure summary chart,     ✅
│                                 one plot function per analysis.py experiment
├── web/                        # GitHub Pages site (static)                   ⬜
│   ├── index.html, app.js, style.css
│   ├── icons/                  # open-licensed SVG (Tabler/Lucide, MIT) or custom
│   └── data/                   # JSON exported by fattree/export.py
├── tests/                      # pytest, 180 tests                            ✅
├── results/  figures/          # committed outputs (README and site use them) ✅
├── README.md  CLAUDE.md  PHASE2_PROMPT.md  requirements.txt  .gitignore       ✅
└── .github/workflows/          # run tests; deploy web/ to Pages              ⬜
```

## 4. Configuration schema (config/default.yaml)

`config/default.yaml` is the canonical, fully-commented schema (not duplicated
here to avoid drift) -- `fattree/config.py`'s `DEFAULTS` dict and
`validate_config()` are its Python-side source of truth. Key sections, all
implemented:
- `allowed_k` -- real switch port counts `[4, 8, 16, 24, 32, 48, 64]`;
  `topology.k` and any `sweep.values` for `topology.k` must come from this list.
- `topology.{k, type}` -- `type: fat-tree | traditional-tree`.
- `failures.{link_pct, switch_pct, link_layers, switch_layers, mode}` -- `*_pct`
  accept a `[min, max]` range to randomize per run (seeded).
- `traffic.*` -- `pattern` (8 options, Sec. 5.2), `size_model`
  (`fixed | mice-elephants | empirical`), `elephant_fraction`,
  `elephant_threshold_bytes`, `subnet_p`/`pod_p` (staggered), `incast_senders`, `load`.
- `routing` -- `single-path | ecmp | two-level | classifier | scheduler`; plus
  `two_level.failover` and `classifier.{max_moves, max_rounds, rearrange_interval_s}`.
- `simulation.level` -- `graph | flow | packet` (packet requires `k <= 8`);
  `packet.*` -- `mtu_bytes, queue_size_packets, propagation_delay_us, transport
  (udp|window), window_packets, timeout_ms, spraying, duration_ms`.
- `link_capacity_gbps, trials, seed, output_dir`.

- CLI flags override config values (e.g. `python run.py -c config/default.yaml --k 16 --link-pct 10`).
- Validate inputs with clear errors (k odd, pct outside [0,100], unknown pattern, ...).
- Every random choice is seeded; trial t uses seed + t.

## 5. Topology facts from the paper (for code and tests)
k = ports per switch (even).
- k pods; each pod has k/2 edge + k/2 aggregation switches; edge <-> agg complete bipartite in the pod.
- (k/2)^2 core switches; each core has exactly one link to every pod.
- Agg switch a (0-based in its pod) connects to core row j = a+1: cores 10.k.(a+1).1 .. 10.k.(a+1).(k/2).
- Hosts k^3/4; switches 5k^2/4; k^3/4 links in each layer (host-edge, edge-agg, agg-core).
- Shortest host paths: same edge 1 path (2 hops); same pod k/2 (4 hops); other pod (k/2)^2 (6 hops).
- Bisection width k^3/8 links -> full bisection bandwidth, 1:1 oversubscription.
- Addressing (§3.2): pod switch 10.pod.switch.1 (switch 0..k/2-1 = edge, k/2..k-1 = agg);
  core 10.k.j.i (j,i in 1..k/2); host 10.pod.switch.ID (ID in 2..k/2+1).
- Fig. 3 check: agg 10.0.2.1 connects to cores 10.4.1.1 and 10.4.1.2.
- Two-level routing (§3.3, Alg. 1): upward port at pod switch z for host ID i is
  (i - 2 + z) mod (k/2) + k/2; core switches hold one /16 per pod.
- Paper typo: Algorithm 2 loops destination pods 0..k/2-1; should be 0..k-1.
- Paper's "ideal bisection bandwidth" in §5 = sum of all hosts' rates (16 x 96 Mb/s), not N/2.

## 6. Metrics (metrics.py)
Structural:
- hosts, switches, links, cables per host vs k (basic)
- switches with >= 2 failed links vs k at fixed p (intermediate);
  theory: 1 - (1-p)^k - k p (1-p)^(k-1)
Connectivity / paths:
- fraction of host pairs still connected; isolated hosts
  (fabric-only failures: edge isolated w.p. p^(k/2))
- avg shortest path length and path stretch (extra hops) among connected pairs
- path diversity: surviving shortest paths / (k/2)^2 for inter-pod pairs
- diameter
Capacity:
- min pod-to-pod max-flow (unit capacities), as a proxy for remaining bisection bandwidth
- worst-case and mean edge/agg oversubscription after failures: (k/2) : surviving uplinks
Routing / load:
- link utilization distribution and max link load per (pattern, routing)
- avg throughput per host (fair share = 1 / max load on path), as % of ideal
- % of flows broken under static two-level routing without rerouting (~1-(1-p)^4 inter-pod)
- compare ECMP vs two-level vs single-path (reproduce the shape of the paper's Table 2)
Failure types:
- link failures vs switch failures at equal %

Each metric needs a test or an analytic sanity check (closed form, or brute force on k=4).

## 7. Visualization
- `visualize.py` (matplotlib): line plots with error bands vs sweep param, one curve per k;
  bar charts (routing comparison); pie charts (link utilization buckets, failure breakdown
  by layer); heatmap (pod x pod connectivity or max-flow). Consistent colors, axis labels with units.
- `web/` (GitHub Pages):
  - Layered topology drawing: core / agg / edge / hosts, pods grouped, server and switch SVG
    icons, IP labels on hover/tooltip.
  - Controls: k, link failure %, switch failure %, traffic pattern, load, routing, seed;
    failed elements drawn red/dashed, links colored by load.
  - Stats panel: hosts, switches, connectivity, avg path length, max load, throughput.
  - Draw individual nodes only for k <= 8; for larger k show a summarized pod-level view.
  - Tab or section with the result charts from figures/.
  - Interactivity approach: **default (a) JS in the browser** recomputes live; keep JS
    topology/failure/routing logic identical to Python and add a test that compares JS and
    Python output for the same seed-independent topology (counts + adjacency for k=4, 6, 8).
    Alternatives considered: (b) static JSON from Python only; (c) Pyodide. Confirm with the
    user before changing approach.

## 8. Phases
**Phase 1, due 22/10 08:00 (initial code + README): done.**
1. `fattree/` package with topology, failures (link + switch), config loading, run.py CLI.
2. tests for topology facts (§5) and failure counts / determinism.
3. README skeleton: overview, paper reference, parameters table, install + run commands,
   planned metrics, AI usage reference.
4. Push to GitHub.

**Phase 2, due 25/10 12:00 (full): partial, see PHASE2_PROMPT.md and README.md
"Not done / future work" for the exact gap.**
5. ✅ traffic.py + routing/ (5 schemes) + metrics.py's one experiment-driving metric
   (the rest of §6 is covered by fattree/sim/graph.py instead of metrics.py).
6. ✅ (4/9 experiments) analysis.py subcommands + visualize.py plots, results/figures
   committed. ⬜ `config/experiments/*.yaml` not used (CLI flags instead, see README);
   5 of 9 PHASE2_PROMPT.md Step 5 experiments not run (collisions_vs_k,
   scheduler_cost_vs_k, mice_vs_elephants, reordering_spraying_vs_hashing, incast).
7. ✅ README results section for the 4 experiments run: setup/parameters + discussion
   and significance each.
8. ⬜ web/ site + GitHub Pages workflow -- not started.

## 9. Conventions
- Python 3.10+, networkx, numpy, matplotlib, pyyaml, pytest (pin in requirements.txt).
- Small documented functions; cite the paper section in docstrings for formulas.
- No hard-coded k anywhere; everything derives from config.
- Keep results reproducible: config + seed fully determines output.
- README must include an "AI usage" section stating which parts were AI-assisted.
