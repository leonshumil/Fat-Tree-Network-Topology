# Fat-Tree Network Topology

Homework 1, course 361.2.2460 — Data Center & AI Networking (BGU, semester A 2026/27).

Implements and analyzes a three-tier fat-tree data center topology, consistent with:

> M. Al-Fares, A. Loukissas, A. Vahdat, "A Scalable, Commodity Data Center Network
> Architecture," SIGCOMM Comput. Commun. Rev., vol. 38, no. 4, pp. 63–74, Oct. 2008,
> doi:[10.1145/1402946.1402967](https://doi.org/10.1145/1402946.1402967).

## Approach

A **graph/flow-level simulation**, not a packet-level emulator like the paper's own
Click-based evaluation — most experiments here use a graph-only or max-min-fair flow
model for speed at large `k`. A genuine discrete-event **packet-level** engine also
exists (`fattree/sim/packet.py`) for small-`k` micro-behavior questions — see
[Simulation levels](#simulation-levels). Built on [networkx](https://networkx.org/):
switches/hosts are graph nodes, links are edges, failures are node/edge removal.

The topology generator (`fattree/topology.py`) reproduces the paper's three-tier
structure and IP addressing (Sec. 3.2), including the agg-to-core-row rule and the
Fig. 3 worked example (`tests/test_topology.py`). Traffic patterns (`fattree/traffic.py`),
five routing schemes including the paper's actual two-level algorithm (`fattree/routing/`),
and three simulation engines (`fattree/sim/`) are layered on top.

## Repository layout

```
fattree/
  topology.py    # build k-ary fat-tree + traditional tree, paper addressing, validate()
  failures.py    # link + switch failures, per layer, exact/bernoulli, fixed or randomized pct
  config.py      # config loading, defaults, validation
  metrics.py     # switches-with-failed-links metric + closed-form theory
  traffic.py     # 8 traffic patterns (paper Sec. 5.2) + flow-size/class models
  routing/       # single-path, ecmp, two-level (paper Alg. 1/2), classifier (Alg. 3), scheduler
  sim/
    graph.py          # structure-only connectivity/path metrics (edge-switch aggregation trick)
    flow.py           # max-min fair rate allocation (progressive filling)
    packet.py         # discrete-event packet simulator engine, k <= 8 only
    transport.py      # udp (open-loop) and tcp_lite (Reno-like, RFC 6298 RTO) state machines
    packet_metrics.py # per-packet/flow/port stats, RFC 4737 reordering
config/default.yaml   # one scenario
run.py           # CLI: build a topology, apply failures, print a summary
visualize.py     # CLI: topology diagram, failure summary chart, experiment plots
analysis.py      # CLI: experiment subcommands -> results/*.csv + figures/*.png
figures/ results/ tests/
```

Not built: the `web/` GitHub Pages interactive site, and 3 of 9 planned experiments —
see [Not done / future work](#not-done--future-work).

## Topology diagram

```bash
python visualize.py --k 8 --link-pct 5 --seed 1 --out figures/topology_k8_failures.png
python visualize.py --k 4 --no-failures --out figures/topology_k4_clean.png
```

Layers (core/agg/edge/host) stacked top to bottom, pods grouped left to right. Failed
links are dashed red; failed switches marked with a red ×.

`k=4`, no failures (matches the paper's Fig. 3 worked example):

![k=4 fat-tree, no failures](figures/topology_k4_clean.png)

`k=8`, 5% of edge-agg/agg-core links failed (seed 1, exact mode — 13 of 256 eligible links):

![k=8 fat-tree, 5% link failures](figures/topology_k8_failures.png)

`visualize.py` also writes a small summary chart: link status pie + before/after
switch/link counts, labeled with the resolved failure percentages:

![k=8 failure summary: link status pie and before/after bar chart](figures/summary_k8_failures.png)

## Traditional tree (comparison baseline)

`topology.type` (`--topology-type`) also accepts `traditional-tree`: a conventional,
oversubscribed 3-tier tree with the *same host count* as the fat-tree (paper Sec. 2 /
Fig. 1) — one agg switch per pod, one core switch total, so there is exactly **one**
path between any two hosts instead of the fat-tree's `(k/2)²` parallel core paths. Hop
counts are identical (2/4/6 for same-edge/same-pod/cross-pod) — redundancy differs, not
path length.

```bash
python visualize.py --k 8 --topology-type traditional-tree --link-pct 5 --seed 1 \
    --out figures/topology_traditional_k8.png --summary-out figures/summary_traditional_k8.png
```

![k=8 traditional tree, 5% link failures — one failed edge-agg link isolates an entire edge switch's hosts](figures/topology_traditional_k8.png)

![k=8 traditional tree failure summary: link status pie and before/after bar chart](figures/summary_traditional_k8.png)

With only 5% of links failed, a single unlucky edge-agg link (dashed red) cuts off an
entire edge switch's hosts — the fat-tree barely notices the same failure rate, since
every edge/agg switch has `k/2` parallel uplinks
(`tests/test_topology.py::test_traditional_tree_has_no_redundant_paths` checks this directly).

## Routing schemes

`fattree/routing/` implements five schemes behind one `Router` interface
(`route(flow)`, `on_failure(...)`, `stats()`):

- **single-path** — one fixed shortest path per destination, never recomputed; a
  failure on that path drops the flow (OSPF-like baseline, no multi-path diversity).
- **ecmp** — hashes each flow onto one of the currently available equal-cost shortest
  paths; recomputes live on the post-failure graph every call, so it reroutes automatically.
- **two-level** — the paper's actual algorithm (Sec. 3.3–3.5, Alg. 1 & 2, with the
  documented destination-pod typo in Alg. 2 fixed). Real prefix/suffix forwarding
  tables per switch; the agg→core port wiring was reverse-engineered from the paper's
  own worked example and verified exactly
  (`tests/test_routing.py::test_two_level_matches_paper_fig3_example`). Static by
  default (`two_level.failover: false`, matching the paper's Sec. 3.1 critique);
  `failover: true` approximates Sec. 3.8's reroute-when-a-choice-exists behavior.
- **classifier** — Alg. 3: new flows go to the least-loaded upward port; periodically,
  up to `classifier.max_moves` flows move from the busiest to the idlest port.
- **scheduler** — Sec. 3.7/4.3: a central scheduler reserves one of the `(k/2)²`
  inter-pod core paths (or `k/2` intra-pod agg paths) per elephant flow
  (`traffic.elephant_threshold_bytes`); mice use two-level.

## Simulation levels

`simulation.level` trades fidelity for speed — a full packet simulation at `k=48`
(27,648 hosts) isn't tractable with a Python discrete-event loop, so each experiment
uses the cheapest level that still answers its question:

- **graph** — structure only, no traffic/routing. `k=48` in under a second via an
  edge-switch aggregation trick (`fattree/sim/graph.py`). Used for
  `critical_failure_pct_vs_k`, `link_vs_switch`. No notion of load/capacity/congestion.
- **flow** — routes every flow, then max-min fair rates (progressive filling) over link
  capacities. No queueing. Used for `table2_reproduction`, `throughput_vs_failures`.
- **packet** — genuine discrete-event sim, integer-ns clock, FIFO drop-tail queues,
  `udp` open-loop or `tcp_lite` Reno-like transport (RFC 6298 RTO), `k ≤ 8` only (slowest
  engine by orders of magnitude). Used for [Incast](#experiment-9-incast) and
  [Mice vs. elephants](#experiment-7-mice-vs-elephants); exercised by
  `tests/test_packet_sim.py`'s 13 validation tests. No SACK, no delayed ACKs, no ECN,
  simplified fast recovery.

## Input parameters

Set via `config/default.yaml` (or any `-c <path>`), overridable on the command line.

| Parameter | Config path | CLI flag | Default | Meaning |
|---|---|---|---|---|
| Switch port count | `topology.k` | `--k` | 8 | Must be in `allowed_k`. Determines `k³/4` hosts, `5k²/4` switches, `k³/4` links/layer. |
| Allowed port counts | `allowed_k` | — | `[4,8,16,24,32,48,64]` | Real switch port counts; `k` and any `sweep.values` must come from this list. |
| Topology type | `topology.type` | `--topology-type` | `fat-tree` | `fat-tree \| traditional-tree`. |
| Link failure % | `failures.link_pct` | `--link-pct` / `--link-pct-range MIN MAX` | 1 | Fixed %, or `[min,max]` range drawn per seeded run. |
| Switch failure % | `failures.switch_pct` | `--switch-pct` / `--switch-pct-range MIN MAX` | 0 | Same, for switches (removes incident links too). |
| Failure mode | `failures.mode` | `--mode` | `exact` | `exact` = `round(pct·N)` chosen without replacement; `bernoulli` = independent per-element probability `p`. |
| Traffic pattern | `traffic.pattern` | `--pattern` | `random` | 8 patterns (Sec. 5.2) — see `fattree/traffic.py`. |
| Flow size model | `traffic.size_model` | — | `mice-elephants` | `fixed \| mice-elephants \| empirical`. |
| Offered load | `traffic.load` | `--load` | 1.0 | Each host sends at `load × link rate`. |
| Routing scheme | `routing` | `--routing` | `two-level` | See [Routing schemes](#routing-schemes). |
| Simulation level | `simulation.level` | — | `flow` | `graph \| flow \| packet` (packet only `k ≤ 8`). |
| Random seed | `seed` | `--seed` | 1 | Trial `t` uses `seed + t`. |
| Trials per point | `trials` | `--trials` | 30 | Repetitions for sweeps. |

Config/CLI are validated with explicit errors (`fattree/config.py`). The full schema
(20+ more keys: `subnet_p`, `incast_senders`, `elephant_fraction`, `two_level.failover`,
`classifier.max_moves`, `packet.*`, ...) is documented inline in `config/default.yaml`.

## Install & run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python run.py -c config/default.yaml                              # default scenario
python run.py -c config/default.yaml --k 16 --link-pct 10          # override params
python run.py -c config/default.yaml --link-pct-range 0 20 --seed 7  # randomized pct
python run.py -c config/default.yaml --topology-type traditional-tree
python run.py -c config/default.yaml --json                        # machine-readable

python visualize.py -c config/default.yaml --out figures/topology.png --summary-out figures/summary.png

# experiments (see Results below for each one's write-up)
python analysis.py switches-multifail --ks 4 8 16 24 32 --p 1 --trials 30 --seed 1
python analysis.py critical-failure-pct --ks 4 8 16 24 32 --trials 10
python analysis.py table2 --k 4 --trials 15
python analysis.py throughput-vs-failures --ks 8 16 --pcts 0 5 10 15 20 25 30 --trials 15
python analysis.py link-vs-switch --k 16 --pcts 0 1 2 5 10 15 20 --trials 15
python analysis.py incast --k 4 --ms 1 2 4 8 15 --queue-sizes 32 64 128 --rounds 10 --trials 5
python analysis.py mice-elephants --k 4 --loads 0.3 0.5 0.7 --trials 3
```

Example output:

```
Topology: fat-tree, k=8
  hosts:    128
  switches: 80
  links:    384
  failures: 0 switches, 3 links (link_pct=1%, switch_pct=0%, exact mode)
  after failure: 80 switches, 381 links
```

## Tests

```bash
python -m pytest tests/ -v
```

194 tests, all passing. Covers: topology counts vs. closed-form formulas for `k ∈
{4,6,8}`; the paper's Fig. 3 addressing example; shortest-path hop counts;
failure-count correctness and determinism under a fixed seed (including randomized
`[min,max]` ranges); config validation; the traditional-tree baseline; the
switches-with-failed-links metric against brute-force enumeration for small `k`; all 8
traffic patterns' structural properties and flow-size class fractions; every routing
scheme (valid/deterministic paths, failure behavior, two-level vs. the paper's Fig. 3
example); and all three simulation levels, including graph-level metrics against
brute-force BFS, flow-level max-min fairness against the classic asymmetric-bottleneck
example, and packet-level exact delay/drop/reorder/retransmission properties.

## Results

### Switches with multiple failed links vs. switch port count

Assignment's "intermediate" example metric (Sec. 5).

**Setup.** For `k ∈ {4,8,16,24,32}`, fail each link independently at `p=1%`
(bernoulli, all three link layers so every switch's `k` ports are equally exposed), 30
seeded trials per `k`, mean ± 95% CI vs. the closed form
`P(≥2 of k ports fail) = 1 - (1-p)^k - k·p·(1-p)^(k-1)`.

`python analysis.py switches-multifail --ks 4 8 16 24 32 --p 1 --trials 30 --seed 1`

![Switches with ≥2 failed links vs. k: simulation mean ± 95% CI tracking the closed-form theory curve](figures/switches_multifail_vs_k.png)

**Discussion.** The simulation tracks the closed-form curve closely at every `k`,
within its 95% CI — validating the failure model's independence assumption. At `k=4` a
switch sees ≥2 failures almost never (≈0.01 switches network-wide); by `k=32` the same
per-link rate yields 50+ such switches on average (a `≈k²p²` effect: choose-2-of-`k`
ports). A switch with ≥2 failed links is the precursor to a structural break — one
failed uplink is absorbed by ECMP's `k/2−1` remaining parallel paths, but multiple
simultaneous losses shrink that fan-out — directly motivating the
[link-vs-switch](#link-failures-vs-switch-failures-at-equal) comparison below.

### Throughput vs. link failure probability, by routing scheme

Assignment's "advanced" example metric (Sec. 5) — throughput as a function of failure
probability, chosen because it captures connectivity *and* capacity loss in one number.

**Setup.** `k ∈ {8,16}`, link failure % ∈ `{0,5,...,30}` (bernoulli), 15 seeded trials,
`random` traffic, flow level: build topology, fail it, build each of 5
routers/configs on the *post-failure* graph, route, measure throughput as % of ideal.

`python analysis.py throughput-vs-failures --ks 8 16 --pcts 0 5 10 15 20 25 30 --trials 15`

![Throughput vs. link failure %, by routing scheme, for k=8 and k=16: ECMP degrades slowest, plain two-level fastest, failover and classifier in between](figures/throughput_vs_failures.png)

**Discussion.** All five schemes decline roughly linearly with failure %, at very
different rates. **ECMP degrades slowest** (k=8: 47.4%→37.6% over 0–30% failure) since
it recomputes shortest paths live, automatically routing around failures. **Plain
two-level degrades fastest** (k=8: 52.0%→22.0%) since its static tables drop a flow
outright rather than try another live upward port. **`failover` recovers much of that
gap** (k=8 at 30%: 22.0%→28.8%), direct evidence for the paper's own Sec. 3.8
motivation. **Classifier stays highest throughout** (better load balancing even at
0% failure); **scheduler tracks close to plain two-level**. Every curve is lower at
`k=16` than `k=8` at the same failure % (more hosts, more hotspots), even though
`k=16` is far more connectivity-resistant per the next experiment — throughput
degrades gracefully long before connectivity collapses.

### Critical link failure % vs. switch port count

**Setup.** For `k ∈ {4,8,16,24,32}`, binary-search (1% resolution) the smallest link
failure % (bernoulli) at which mean host-pair connectivity drops below 99.9%, and
separately 99% (10 seeded trials per candidate, graph level).

`python analysis.py critical-failure-pct --ks 4 8 16 24 32 --trials 10`

![Critical link failure % vs. k: both the 99.9% and 99% connectivity thresholds rise steeply with k](figures/critical_failure_pct_vs_k.png)

**Discussion.** The critical % rises sharply with `k`: ~3.5% at `k=4` to ~61%
(99.9% threshold) / ~72% (99%) at `k=32`. This is the flip side of the previous
experiment — multi-failure-switch exposure grows with `k`, but host-pair connectivity
is far *more* robust at large `k`, since disconnecting a host pair needs wiping out
*all* `(k/2)²` core paths, and `(k/2)²` grows quadratically while independent failures
only erode that redundancy linearly in expectation. Adding ports doesn't just add
hosts — it adds disproportionately more fault tolerance per unit failure probability.

### Link failures vs. switch failures at equal %

**Setup.** At `k=16`, `pct ∈ {0,1,2,5,10,15,20}`, compare pure link failures
(edge-agg + agg-core) vs. pure switch failures (edge + agg) at the *same* %
(bernoulli, 15 trials each, graph level): fraction connected, and inter-pod path
diversity (pods 0/1, surviving shortest paths / `(k/2)²`).

`python analysis.py link-vs-switch --k 16 --pcts 0 1 2 5 10 15 20 --trials 15`

![Link vs switch failures at equal %: switch failures cause far steeper connectivity loss; path diversity degrades similarly for both, with link failures slightly worse at high %](figures/link_vs_switch_failures.png)

**Discussion.** Switch failures are far more damaging to *connectivity*: at 20%
switch failure, connectivity drops to ~63%, while 20% *link* failure leaves it
essentially untouched (>99.9%) — each switch failure's "blast radius" removes `k` or
`k/2` links at once (plus, for an edge switch, isolates its hosts outright). On *path
diversity* the two curves are closer, with link failures slightly worse at high % (they
spread evenly across every one of the `(k/2)²` paths, while switch failures are
"lumpier"). The takeaway: link and switch failure rates are not interchangeable risk
signals, and a monitoring policy tracking only link-level rate underestimates
switch-failure risk.

### Table 2 reproduction (paper Sec. 5, Table 2)

**Setup.** `k=4`, link rate 96 Mb/s (paper's value), flow level, 10 traffic patterns ×
4 "scheme"s (topology+routing combos: **tree** = `traditional-tree` + single-path;
**two-level/classifier/scheduler** = fat-tree with that routing) × 15 trials,
throughput as % of ideal.

`python analysis.py table2 --k 4 --trials 15` (black ticks in the plot = paper's own values)

![Table 2 reproduction: grouped bars (our simulation) vs. black ticks (paper's values), by pattern and scheme](figures/table2_reproduction.png)

**Discussion.** The *shape* reproduces well: `tree` is clearly worst on adversarial
patterns (`inter-pod-incoming`: 8.3% vs. paper's 28%), `scheduler` is the most
consistently strong performer, exactly as the paper argues. Absolute numbers diverge
on the worst-case patterns for three real reasons, not a bug: (1) our
`traditional-tree` isn't calibrated to the paper's specific 3.6:1 hardware ratio; (2)
our classifier/scheduler are simplified approximations, not literal
re-implementations; (3) the paper measures over real packet-level Click emulation,
this is a single max-min-fair flow-level snapshot. The patterns with no exotic hotspot
structure (`random`, `stride1`/`staggered(1.0,0.0)`) land close to or exactly match the
paper's values — the strongest evidence the underlying model is sound and the
divergence elsewhere is a fidelity gap, not a correctness bug.

### Experiment 9: Incast

**In plain words.** M near-simultaneous responses (e.g. a fan-out query) converge on
one requester. The fat-tree's full bisection bandwidth says nothing about the *last*
hop — all M senders funnel into the same single link into the receiver's edge switch.
That one port's queue overflows, TCP loses packets, and a 200ms RFC-6298-style minimum
RTO (vs. a true RTT of tens of *microseconds* here) makes goodput collapse while the
sender waits out a wildly oversized timeout.

**Setup.** `k=4`, `tcp_lite`, barrier-synchronized rounds (round r+1 starts once round
r's M responses all finish; each round uses fresh TCP/queue state). `block_bytes=64KB`,
`rounds=10`, 5 trials, `M ∈ {1,2,4,8,15}`.

`python analysis.py incast --k 4 --ms 1 2 4 8 15 --queue-sizes 32 64 128 --rounds 10 --trials 5`

![Incast goodput collapse vs. M, one curve per receiver buffer size: the classic incast cliff, shifting right as the buffer grows](figures/incast_goodput_vs_m_buffer.png)

**Discussion (buffer size).** Textbook incast collapse: a 32-packet buffer craters
from 87.5% (M=1) to 0.5% at M=2; a 64-packet buffer survives to M=2 (93.3%) but
collapses at M=4 (1.7%); 128 packets survives to M=4 (96.5%) before collapsing at M=8.
Bigger buffers only move the cliff, they don't remove it.

![Incast goodput vs. M, min RTO 200ms vs 1ms](figures/incast_goodput_vs_m_rto.png)

**Discussion (min RTO).** At M=4, dropping min RTO from 200ms to 1ms raises mean
goodput from 1.7% to 47.3% (high variance — recovery is timing-sensitive); at M=15,
1ms goodput (9.7%) is ~50× the 200ms case. Direct reproduction of Vasudevan et al.'s
(SIGCOMM 2009) claim: the 200ms floor is a WAN-TCP vestige mismatched to sub-ms
datacenter RTT — most of the collapse is a timer problem, not a capacity problem.

![Incast goodput vs. M: fat-tree vs. traditional tree](figures/incast_goodput_vs_m_topology.png)

![Where incast packets are dropped, by layer: fat-tree vs. traditional tree, at M=15](figures/incast_drop_location.png)

**Discussion (topology).** Fat-tree and traditional-tree collapse at essentially the
same M — the richer core doesn't help, since the bottleneck was never the core. The
drop-location split shows why differently: fat-tree drops 84% at the receiver's own
last-hop port and 16% on an agg link; the traditional tree drops 100% at its single
oversubscribed agg-to-core link, a second bottleneck on top of the universal last-hop one.

![RTO timeouts per round vs. M, by buffer size](figures/incast_timeouts_vs_m.png)

![Receiver edge-port queue length over one round, M=15](figures/incast_queue_series.png)

**Discussion (timeouts/queue).** Timeouts climb steeply with M past each buffer's
collapse point, tracking the goodput collapse directly — confirming timeouts, not just
drops, are the proximate cause (a drop alone costs a retransmit; a timeout costs the
full, possibly-doubled RTO). The queue trace for one M=15 round shows the mechanism:
instant buffer fill, then sparse ~200ms-spaced blips as each lost flow's RTO-paced
retry trickles in.

`tcp_lite` lacks SACK, delayed ACKs, ECN, and full fast recovery — this makes the
simulated collapse somewhat more severe than a production DCTCP/SACK stack, but the
qualitative cliff/RTO/topology findings match real incast research.

### Experiment 7: Mice vs. elephants

**The question.** Datacenter traffic is bimodal: many small "mouse" flows and a few
huge "elephant" flows carrying most of the bytes. The paper's classifier (§3.6) and
scheduler (§3.7) both exist to keep elephants from starving mice. Do they?

**Setup and an important simplification.** `k=4`, `tcp_lite` (`min_rto_ms=1`), Poisson
flow arrivals per host (`generate_poisson_flows`) at `load ∈ {0.3,0.5,0.7}`, sizes from
`mice-elephants` (mice 2–100KB, elephants 1–10MB, `elephant_fraction=0.1`), 3 trials per
point. Flows are generated over a window longer than what's measured (a `tail_ms`
buffer) so a flow landing late in the generation window still gets a fair chance to
finish, avoiding a window-edge artifact that would otherwise look like congestion.

The spec asks for classifier's rearrangement and scheduler's elephant-detection to run
as *live hooks inside the packet engine*; this implementation instead uses
`fattree/routing/precomputed.py`'s `PrecomputedPathRouter`, which decides each flow's
path **once, at arrival, from the already-known batch** (`classifier.route_all()` for
classifier; sequential reservation with *estimated* release for scheduler, since true
release needs the simulation's own output — a circular dependency). single-path/ecmp/
two-level are unaffected (already live, per-flow). This is the single most important
caveat on everything below.

`python analysis.py mice-elephants --k 4 --loads 0.3 0.5 0.7 --trials 3`

![Mean slowdown by scheme, mice vs. elephants, load=0.5: scheduler has the lowest mouse slowdown but no completed elephants (N/A)](figures/mice_elephants_slowdown_bars.png)

![Mouse p99 (tail) slowdown by scheme, load=0.5](figures/mice_elephants_mouse_p99_bars.png)

![Mouse p99 slowdown vs. load, by scheme](figures/mice_elephants_mouse_p99_vs_load.png)

![Elephant goodput vs. load, by scheme](figures/mice_elephants_elephant_goodput_vs_load.png)

**Discussion (slowdown and tail latency).** At load=0.5, mean mouse slowdown ranges
from 22.7× (single-path) to 5.8× (scheduler) — far from "near 1" since at `k=4` with
10%-elephant Poisson traffic, this is sustained heavy congestion, not the light-load
regime the low-load validation test checks. **Scheduler gives mice the lowest
slowdown at every load** (2.1×/5.8×/8.2× at 0.3/0.5/0.7), consistent with its purpose.
**Tail latency (p99) is where the spread is starkest**: single-path reaches 545–713×
at loads 0.5–0.7 (mice stuck behind a long-lived elephant with nowhere else to go),
while scheduler stays far lower (3.4×/7.9×/151×). Classifier's p99 is erratic (127× at
load=0.5) despite a reasonable mean — the clearest symptom of the precomputed-batch
simplification: its least-loaded-port decision is made once from a snapshot, not the
actual continuously-changing queue state a live classifier would react to.

**Discussion (elephants).** Elephant goodput at load=0.5 ranges from 283 Mb/s
(single-path) to 604 Mb/s (classifier). **Scheduler's own elephants are the clear
failure case: zero completed at any load** ("N/A" in the bar chart) — a structural
issue, not a bug: at `k=4` there are only 4 inter-pod core paths and 2 intra-pod agg
paths for dozens of elephants per trial, and a "reserved" path still shares its
physical links with reservation-unaware two-level mouse traffic. A direct illustration
of why the paper's design assumes reservation state scales with network size.

![CDF of mouse FCT by scheme, load=0.7](figures/mice_elephants_mouse_fct_cdf.png)

**Discussion (CDF).** At load=0.7, single-path's CDF lags past the 90th percentile and
stretches past 400ms, while scheduler/classifier/two-level cluster tighter near the low
end — the CDF makes visible what the p99 bars summarize as one number.

![Share of flows vs. share of bytes by class: ~10% of flows (elephants) carry ~93% of bytes](figures/mice_elephants_bytes_pie.png)

**Discussion (bytes).** With `elephant_fraction=0.1`, elephants are 10% of flows but
93% of bytes — the textbook "few flows, most bytes" shape that motivates treating the
two classes differently: per-flow fairness would starve elephants' bandwidth needs,
per-byte fairness would let elephants dominate every mouse's queue.

## Not done / future work

9 experiments were planned across `PHASE2_PROMPT.md`/`PHASE3_*.md`; 6 are done (the 4
Phase 2 experiments above, Incast, Mice vs. elephants). Remaining:

- **collisions_vs_k** — throughput lost to two-level/ECMP hash collisions vs. `k`;
  mechanically similar to `table2`/`throughput_vs_failures`, blocked on time only.
- **scheduler_cost_vs_k** — time-per-placement and link/flow-state memory vs. `k`
  (paper Table 3); `FlowSchedulerRouter.stats()` already records this, only the
  sweep/plot script is missing.
- **Experiment 8 (reordering: spraying vs. per-flow hashing)** — the packet engine
  already supports spraying and `tests/test_packet_sim.py` already demonstrates the
  reordering it causes; needs its own `analysis.py` subcommand.
- **Live classifier/scheduler rearrangement inside the packet engine** — the single
  biggest simplification in this submission (see Experiment 7 above); a second major
  engine extension on the scale of the packet-engine rewrite.
- **`web/`** GitHub Pages interactive site — not started.
- `build_traditional_tree`'s oversubscription is fixed by its structure, not a tunable
  parameter matching the paper's exact 3.6:1 hardware ratio.
- `fattree/metrics.py`'s §6 list beyond `switches_with_multiple_failed_links` is not
  separately implemented, though most of it exists via `fattree/sim/graph.py`.

AI assistance (Claude, Anthropic) was used throughout development, from a specification
written by the author; the author has reviewed and understands every line submitted.
