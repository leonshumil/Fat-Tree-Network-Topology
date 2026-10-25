# Fat-Tree Network Topology

Homework 1, course 361.2.2460 — Data Center & AI Networking (BGU, semester A 2026/27).

Three findings drive this submission: (1) a fat-tree's survival under random failures is
governed almost exactly by simple closed-form formulas, and bigger `k` buys
disproportionately more fault tolerance; (2) even with **zero** failures, per-flow hash
routing (ECMP/two-level) wastes roughly half of the network's promised bisection
bandwidth to hash collisions — a real gap from the paper's own Sec. 5.3 expectation, and
the exact motivation for the same authors' later Hedera scheduler; (3) incast collapses
abruptly at a predictable number of senders, and the cause is almost entirely a 200ms
TCP timeout, not a lack of bisection bandwidth — the fat-tree's rich core does essentially
nothing to help, because the bottleneck is the receiver's own last cable.

> M. Al-Fares, A. Loukissas, A. Vahdat, "A Scalable, Commodity Data Center Network
> Architecture," SIGCOMM Comput. Commun. Rev., vol. 38, no. 4, pp. 63–74, Oct. 2008,
> doi:[10.1145/1402946.1402967](https://doi.org/10.1145/1402946.1402967).

## The topology

A `k`-ary fat-tree (paper Sec. 3): `k` pods, each with `k/2` edge and `k/2` aggregation
switches (complete bipartite within the pod), `(k/2)²` core switches each wired to every
pod via one specific "row." Built in `fattree/topology.py` on top of
[networkx](https://networkx.org/) — switches/hosts are graph nodes, links are edges,
failures are node/edge removal — with the paper's own IP addressing scheme (Sec. 3.2),
verified against its Fig. 3 worked example. `topology.type` also accepts
`traditional-tree`: one agg switch per pod and a single global core switch (same host
count, zero redundancy above the edge layer), used as the non-fat-tree comparison in
[Experiment 3](#experiment-3-incast).

| | formula | k=4 | k=8 | k=16 |
|---|---|---|---|---|
| hosts | k³/4 | 16 | 128 | 1024 |
| switches | 5k²/4 | 20 | 80 | 320 |
| links per layer | k³/4 | 16 | 128 | 1024 |
| core switches | (k/2)² | 4 | 16 | 64 |
| bisection width | k³/8 | 8 | 64 | 512 |
| hop count | 2 / 4 / 6 | same-edge / same-pod / cross-pod |

`k=4`, no failures (matches the paper's Fig. 3):

![k=4 fat-tree, no failures](figures/topology_k4_clean.png)

`k=8` traditional tree, 5% link failures — one failed edge-agg link isolates an entire
edge switch's hosts (no redundant path exists, unlike the fat-tree):

![k=8 traditional tree, 5% link failures](figures/topology_traditional_k8.png)

```bash
python visualize.py --k 8 --link-pct 5 --seed 1 --out figures/topology_k8.png
python visualize.py --k 8 --topology-type traditional-tree --link-pct 5 --seed 1 \
    --out figures/topology_traditional_k8.png
```

## Install & run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests/ -v          # 198 tests

# Experiment 1: Resilience -- what breaks, and how much it matters
python analysis.py e1-resilience --ks 8 16 32 \
    --pcts 0 5 10 15 20 25 30 35 40 45 50 55 60 --trials 30 \
    --layer-k 16 --switch-pcts 5 10

# Experiment 2: Speed under failures, and the hash-collision ceiling
python analysis.py e2-throughput --k 16 --pcts 0 5 10 15 20 25 30 \
    --ks 4 8 16 24 32 --trials 20

# Experiment 3: Incast
python analysis.py e3-incast --ms 1 2 4 8 15 --queue-sizes 32 64 128 \
    --time-buffer 64 --compare-m 8 --rounds 20 --trials 10
```

Each writes `results/eN_*.csv` + `figures/eN_*.png`. See
[Other analyses available in the code](#other-analyses-available-in-the-code) for
additional subcommands not part of this submission's presented results.

## Input parameters

Set via `config/default.yaml` (or `config/experiments/e{1,2,3}_*.yaml`, which document
each experiment's fixed parameters), overridable on the command line.

| Parameter | Config path | CLI flag | Default | Meaning |
|---|---|---|---|---|
| Switch port count | `topology.k` | `--k` | 8 | Must be in `allowed_k`. Determines `k³/4` hosts, `5k²/4` switches, `k³/4` links/layer. |
| Allowed port counts | `allowed_k` | — | `[4,8,16,24,32,48,64]` | Real switch port counts; `k` and any `sweep.values` must come from this list. |
| Topology type | `topology.type` | `--topology-type` | `fat-tree` | `fat-tree \| traditional-tree`. |
| Link failure % | `failures.link_pct` | `--link-pct` / `--link-pct-range MIN MAX` | 1 | Fixed %, or `[min,max]` range drawn per seeded run. |
| Switch failure % | `failures.switch_pct` | `--switch-pct` / `--switch-pct-range MIN MAX` | 0 | Same, for switches (removes incident links too). |
| Failure mode | `failures.mode` | `--mode` | `exact` | `exact` = `round(pct·N)` chosen without replacement; `bernoulli` = independent per-element probability `p`. |
| Traffic pattern | `traffic.pattern` | `--pattern` | `random` | 8 patterns (Sec. 5.2) — see `fattree/traffic.py`. |
| Offered load | `traffic.load` | `--load` | 1.0 | Each host sends at `load × link rate`. |
| Routing scheme | `routing` | `--routing` | `two-level` | `single-path \| ecmp \| two-level \| classifier \| scheduler`. |
| Simulation level | `simulation.level` | — | `flow` | `graph \| flow \| packet` (packet only `k ≤ 8`) — see below. |
| Random seed | `seed` | `--seed` | 1 | Trial `t` uses `seed + t`. |
| Trials per point | `trials` | `--trials` | 30 | Repetitions for sweeps. |

Config/CLI are validated with explicit errors (`fattree/config.py`). The full schema
(`incast.*`, `packet.*`, `classifier.*`, ...) is documented inline in
`config/default.yaml` and `config/experiments/*.yaml`.

## Simulation levels

`simulation.level` trades fidelity for speed — a full packet simulation at `k=48`
(27,648 hosts) isn't tractable with a Python discrete-event loop, so each experiment
uses the cheapest level that still answers its question:

- **graph** (`fattree/sim/graph.py`) — structure only, no traffic/routing; `k=48` in
  under a second via an edge-switch aggregation trick. **Used by Experiment 1**, which
  only asks "are these two hosts still connected," not "how fast."
- **flow** (`fattree/sim/flow.py`) — routes every flow, then computes max-min fair rates
  (progressive filling) over link capacities; no queueing. **Used by Experiment 2**,
  which needs real contention/sharing behavior at `k` up to 32 — intractable at the
  packet level, but flow level answers "how much throughput" exactly.
- **packet** (`fattree/sim/packet.py`) — genuine discrete-event simulation,
  integer-nanosecond clock, FIFO drop-tail queues, `tcp_lite` (Reno-like, RFC 6298 RTO,
  RFC 5681 go-back-N recovery), restricted to `k ≤ 8`. **Used by Experiment 3**, which is
  specifically about queueing, drops, and timeouts — none of which graph or flow level
  can represent at all.

## Results

### Experiment 1: Resilience — what breaks, and how much it matters

**Question.** When random parts of the network fail, when do servers lose contact with
each other, and does it matter *which layer* fails?

**Setup.** Part A: `k ∈ {8,16,32}`, link failure % ∈ `{0,5,...,60}` (bernoulli, fabric
links only), 30 trials/point — fraction of host pairs connected (`fattree/sim/graph.py`)
and inter-pod path diversity (surviving shortest paths / `(k/2)²`), each against a
theory curve. Part B: `k=16` fixed, 5%/10% switch failure in *one* layer at a time
(edge-only / agg-only / core-only), 30 trials.

```bash
python analysis.py e1-resilience --ks 8 16 32 --pcts 0 5 ... 60 --trials 30
```

![Bigger fat-trees survive far more broken links: connectivity vs. link failure %, one line per k, with the 1-2p^(k/2) theory curve dashed](figures/e1_connectivity.png)

**Discussion.** The theory curve `1 - 2p^(k/2)` — a server is cut off only once *all*
`k/2` uplinks of its edge switch fail — tracks the simulation closely at every `k`.
`k=8` drops below 99.9% connectivity at just 15% link failure; `k=16` survives to 45%;
`k=32` never drops below 99.9% anywhere in the tested range (still 99.95% connected at
60% failure). This is the central resilience argument for higher-radix fat-trees: a
server's isolation probability falls *exponentially* in `k/2`, so each doubling of port
count buys a dramatically larger safe operating range, not just more hosts.

![Redundancy erodes long before connectivity is lost: surviving inter-pod paths / (k/2)^2 vs. link failure %, with the (1-p)^4 theory curve](figures/e1_path_diversity.png)

**Discussion.** Path diversity (a path survives only if all 4 of its fabric links
survive, hence `(1-p)^4`) collapses far faster than connectivity: half of the `(k/2)²`
inter-pod paths are already gone by ~16-20% link failure — well before any of the three
`k` values has lost a single percentage point of connectivity. This is the mechanism
*behind* the first figure: the fat-tree stays connected long after it has stopped being
genuinely redundant, because losing most of the parallel paths between two pods still
leaves at least one.

![An edge failure cuts off its own servers; core/agg barely touch connectivity: % of server pairs disconnected by failed-switch layer, k=16](figures/e1_layer.png)

**Discussion.** At `k=16`, failing 5%/10% of *edge* switches disconnects 10.8%/20.2% of
server pairs (close to the `2x` relationship expected from "a pair is disconnected if
either endpoint is isolated," since each lost edge switch directly orphans its own
hosts) — while failing the *same percentage* of agg or core switches disconnects
**exactly 0%** of pairs, because every agg/core switch has `k/2` redundant peers the
fat-tree can route around instantly. Layer matters enormously more than raw failure
percentage; a monitoring policy that only tracks "% of switches down" without knowing
which layer is blind to the one failure mode that actually costs connectivity.
**Limitation:** this experiment measures connectivity only, not bandwidth — a fully
connected network can still be badly congested, which is exactly what Experiment 2 covers.

### Experiment 2: Speed under failures, and the hash-collision ceiling

**Question.** How much of full line-rate speed do servers actually get, with and
without failures, and how much does the routing scheme change that?

**Setup.** Flow level (max-min fair), random permutation traffic, every host sending at
line rate, fixed flow size. Three schemes: two-level (paper), two-level with
`failover: true`, and ECMP. Part A: `k=16`, link failure % ∈ `{0,5,...,30}`, 20 trials.
Part B: 0% failures, `k ∈ {4,8,16,24,32}`, 20 trials.

```bash
python analysis.py e2-throughput --k 16 --pcts 0 5 ... 30 --ks 4 8 16 24 32 --trials 20
```

![Static routing tables lose the most as links fail: throughput (% of ideal) vs. link failure %, one line per scheme, k=16](figures/e2_vs_failures.png)

**Discussion.** All three schemes decline as links fail, but at very different rates.
**Plain two-level degrades fastest** (46.5%→17.3%, a 62.8% relative drop): its tables are
static, so a flow whose fixed hop crosses a failed link is simply dropped rather than
rerouted. **ECMP degrades slowest** (42.8%→34.5%, only 19.4% relative): it recomputes
shortest paths live on every call, so it reroutes around a failure automatically
whenever an equal-cost alternative survives. **Enabling `two_level.failover`** recovers
much of the gap (22.8% vs. 17.3% at 30% failure, a 5.5-point recovery) by retrying
another live upward port instead of dropping outright — direct, quantitative support for
the paper's own Sec. 3.8 motivation for adding failover to an otherwise-static scheme.

![Hash collisions waste about half the capacity, even with no failures: throughput at 0% failures vs. k, one line per scheme, with the random-hashing theory curve dashed](figures/e2_collision_ceiling.png)

**Discussion.** Even at **0% failures**, no scheme gets close to the 100% full
bisection bandwidth promises. ECMP achieves only 56.6% at `k=4`, falling to 40.9% at
`k=32`; two-level does somewhat better (64.4%→44.7%, since its ID-based deterministic
placement happens to spread a full permutation's load slightly more evenly than ECMP's
independent random hash per flow) — and `two-level` with `failover` is numerically
**identical** to plain `two-level` here, exactly as expected, since failover only
changes behavior when something is actually broken. The analytic reference curve
`1 - (1-2/k)^(k/2)` — the expected fraction of upward ports used when `k/2` flows hash
independently onto `k/2` ports *at one switch* — sits visibly *above* every measured
curve (75.0%→64.4%), because real routing compounds collisions across **multiple**
independent hash stages (edge→agg, then agg→core) along a path, not just one. An
independent re-implementation (`tests/test_e2_throughput.py`, using networkx's own path
enumeration and a freshly-written max-min solver, no shared code with
`fattree/routing`/`fattree/sim/flow`) reproduces `k=8`'s ~47% within statistical noise,
confirming this isn't a pipeline bug. This is a direct, quantitative contradiction of
the paper's own Sec. 5.3 expectation that collisions would be rare, and it is exactly
the gap the same authors' later paper, Hedera (NSDI 2010), built a central flow
scheduler to close.

### Experiment 3: Incast

**Question.** When many servers answer one server at nearly the same moment
(partition/aggregate — a search fan-out, an AI all-reduce), why does throughput
collapse, what actually controls the collapse point, and does the fat-tree's full
bisection bandwidth help at all?

**In one number.** 8 senders × 64 KB = 512 KB arriving at a 96 KB (64-packet) receiver
buffer. Sending that much data at line rate takes only ~4.2ms — but one lost packet
means a 200ms minimum RTO wait. 4.2 / (4.2 + 200) ≈ 2% goodput, and that is almost
exactly what was measured (1.8%).

**Setup.** Packet level, `tcp_lite` (fixed in this submission, see
[Limitations](#limitations)), `k=4`, `M ∈ {1,2,4,8,15}` senders, 64 KB block per sender
per round, 20 barrier-synchronized rounds, 10 trials, buffer ∈ `{32,64,128}` packets,
min RTO ∈ `{200ms, 1ms}`, each sender's request jittered by a few random microseconds
(`incast.start_jitter_us`) so the burst isn't an unrealistic perfect lockstep collision.

```bash
python analysis.py e3-incast --ms 1 2 4 8 15 --queue-sizes 32 64 128 \
    --time-buffer 64 --compare-m 8 --rounds 20 --trials 10
```

![A bigger buffer only delays the incast collapse: goodput (% of receiver line rate) vs. number of senders M, one line per buffer size, min RTO 200ms](figures/e3_collapse.png)

**Discussion.** Textbook incast collapse, and the collapse point is predictable: after
one RTT of slow start a sender's window is `2 × init_cwnd` (= 20 packets here), so a
buffer overflows once `M × 2 × init_cwnd` exceeds it, i.e. `M* ≈ buffer / (2×init_cwnd)`
— `32→1.6`, `64→3.2`, `128→6.4`. The measured collapse lands at **M=2, 4, 8** for
buffers 32, 64, 128 respectively — matching the rounded-up analytic estimate exactly. A
bigger buffer doesn't prevent collapse, it only moves the cliff to a larger `M`.

![A 200ms timeout costs far more than the data itself ever takes to send: mean round time split into sending data vs. waiting for timeouts, buffer=64, min RTO 200ms vs. 1ms](figures/e3_where_time_goes.png)

**Discussion.** At `M=8`, sending the data itself takes 4.2ms — but with a 200ms
minimum RTO, the *mean round time* is 230ms, i.e. **226ms of pure timeout waiting**.
Dropping the minimum RTO to 1ms cuts that same round's waiting time to under 2ms — a
>100x reduction — with send time completely unchanged, because the data was never the
problem. This is a direct, quantitative reproduction of Vasudevan et al.'s (SIGCOMM
2009) central claim: the 200ms RTO floor is a wide-area-network TCP default, wildly
mismatched to a sub-millisecond datacenter RTT, and most of incast's cost is a *timer*
problem, not a *capacity* problem.

![You can see the buffer fill and overflow: queue length at the receiver's edge port over one round, M=2 vs. M=8, buffer=64, drops marked with x](figures/e3_queue.png)

**Discussion.** At `M=2` the queue never reaches the 64-packet buffer limit and drains
cleanly in about 1ms. At `M=8`, the queue fills to the buffer limit within ~0.8ms,
plateaus (new arrivals are dropped the instant anything leaves, visible as the `x`
marker), and only drains ~1.3ms later once the senders' windows have collapsed — the
entire fill-plateau-drain cycle takes under 3ms, dwarfed by the 200ms timeout that
follows it (not shown on this zoomed-in panel; see the previous figure). The inset
compares fat-tree vs. traditional-tree goodput at `M=8`: **1.8% vs. 1.8%, identical**.
The fat-tree's richer core does not help at all, because the bottleneck was never the
core — it is the receiver's own single last-hop cable, which has exactly the same
capacity regardless of how many parallel paths exist upstream of it.

**Real-world fixes** this motivates (not implemented here): bigger receiver buffers
(delays but doesn't prevent collapse, per above), a much smaller RTO (the direct fix
demonstrated here), DCTCP-style ECN marking to signal congestion before a drop is
needed at all, or receiver-driven transport protocols (Homa, NDP) that sidestep the
sender-paced-retransmission model entirely.

## Limitations

- **`tcp_lite` is not real TCP.** No SACK (a receiver can only signal "give me
  everything from this sequence number on," not "I'm specifically missing these"), no
  delayed ACKs, no ECN, simplified fast recovery (`cwnd` set directly to the new
  `ssthresh`, no inflation during recovery). Fixed in this submission: go-back-N timeout
  recovery (RFC 5681) and a separate RTO backoff multiplier that resets on forward
  progress (RFC 6298) — see `fattree/sim/transport.py`'s module docstring.
- **Packet level is `k ≤ 8` only.** A full discrete-event simulation at larger `k` is
  not tractable in Python in reasonable time; Experiment 3 is deliberately scoped to
  `k=4` for this reason (PHASE4.md's optional `k=8` pass was not run, for time).
  Experiment 2 similarly needed two real performance fixes (`fattree/routing/ecmp.py`,
  `fattree/sim/flow.py`) to make its `k=32` flow-level sweep tractable at all.
- **`build_traditional_tree`'s oversubscription is fixed by its structure**, not a
  tunable parameter matched to any specific industry hardware ratio.
- **Classifier and scheduler routing schemes exist in the code** (`fattree/routing/`)
  but are not part of this submission's 3 presented experiments — see below.

## Tests

```bash
python -m pytest tests/ -v
```

198 tests, all passing. Validation highlights relevant to this submission: exact
packet-level delay/drop/reordering formulas and an M/D/1 queueing-theory check
(`tests/test_packet_sim.py`); a sent = delivered + dropped conservation invariant under
load; the Phase 4 TCP fix specifically — a 10-packet burst loss recovers with exactly 1
timeout (not 10), RTO backoff returns to its base value once forward progress resumes,
and an incast smoke test confirms a 1ms minimum RTO gives ≥5x the goodput of 200ms;
`tests/test_e2_throughput.py`'s independent re-implementation of the collision-ceiling
result (different path-enumeration code, different max-min solver, lands within 3 points
of ~47% at k=8); and the underlying topology/failure/routing/flow-level machinery each
experiment is built on (closed-form counts, the paper's own Fig. 3 addressing example,
brute-force connectivity checks, max-min fairness against the classic
asymmetric-bottleneck example).

## Other analyses available in the code

Earlier development produced additional experiments whose code and tests still work,
but which are not part of this submission's 3 presented results (figures/results not
committed, to keep this README focused — rerun any of them with `python analysis.py
<subcommand>`): `switches-multifail` (switches with ≥2 failed links vs. k, the
assignment's own "intermediate" example metric), `critical-failure-pct` (smallest link
failure % that drops connectivity below 99.9%/99%, superseded by Experiment 1's theory
comparison), `link-vs-switch` (link vs. switch failures at equal %, superseded by
Experiment 1 Part B), `table2` (reproduction of the paper's Table 2 across 10 traffic
patterns × 4 scheme/topology combinations), `throughput-vs-failures` (5-scheme
comparison including classifier/scheduler, superseded by Experiment 2), `incast` (the
original 6-figure incast sweep, superseded by Experiment 3), and `mice-elephants`
(FCT/slowdown of mice vs. elephants by routing scheme, including a packet-level
`PrecomputedPathRouter` adapter for the batch-oriented classifier/scheduler schemes).

## References

1. M. Al-Fares, A. Loukissas, A. Vahdat, "A Scalable, Commodity Data Center Network
   Architecture," SIGCOMM 2008. doi:[10.1145/1402946.1402967](https://doi.org/10.1145/1402946.1402967)
2. M. Al-Fares, S. Radhakrishnan, B. Raghavan, N. Huang, A. Vahdat, "Hedera: Dynamic
   Flow Scheduling for Data Center Networks," NSDI 2010.
3. V. Vasudevan et al., "Safe and Effective Fine-grained TCP Retransmissions for
   Datacenter Communication," SIGCOMM 2009.
4. RFC 5681 (TCP Congestion Control), RFC 6298 (Computing TCP's Retransmission Timer),
   RFC 4737 (Packet Reordering Metrics).

## AI usage

Developed with AI assistance (Claude, Anthropic) from specifications written by the
author (`CLAUDE.md`, `PHASE2_PROMPT.md`, `PHASE3_*.md`, `PHASE4.md`), covering the
simulation core, routing schemes, three simulation engines, analysis/visualization code,
and the pytest suite. Two real performance bugs (ECMP's per-flow path recomputation,
the max-min-fair solver's full-rescan loop) and two real correctness bugs in TCP-lite's
timeout recovery (single-packet-only retransmission after a timeout, RTO backoff that
could stay stuck after Karn's rule skipped an RTT sample) were found and fixed during
this process, each traced to a specific observed symptom (slow k=32 sweeps; incast
goodput far below what the fix predicted) rather than found by inspection alone. The
author has reviewed and understands every line submitted and is responsible for its
correctness.
