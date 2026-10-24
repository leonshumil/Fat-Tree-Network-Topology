# Phase 2 prompt for Claude Code

Read CLAUDE.md first. Phase 1 is done and pushed (fattree/ package, config, run.py,
failures, traditional-tree baseline, first metric, 63 passing tests). Do not break existing
tests or the CLI. Deadline for everything below: Monday.

Work in this order. After each step: run `python -m pytest -q`, commit with a clear message,
and give me a short summary before moving on. Ask me before changing any decision written here.

---

## Step 1 – Allowed k values
- Add `allowed_k: [4, 8, 16, 24, 32, 48, 64]` to config/default.yaml (real switch port counts).
- Config, CLI and sweeps accept only values from this list, with a clear error message
  otherwise (e.g. `k=25 is not allowed; choose one of 4, 8, 16, 24, 32, 48, 64`).
- Sweeps over k (`sweep.param: topology.k`) must use values from the list only.
- Tests: rejected values (odd, 25, 47, 2), accepted values, sweep validation.

## Step 2 – Traffic module (fattree/traffic.py)
Traffic = set of flows `(src_host, dst_host, size_bytes, start_time, class)`.

Patterns (paper §5.2, all permutations unless noted):
- `random`: random derangement (every host sends to exactly one other host, receives from exactly one)
- `stride`: host x -> host (x + i) mod N, `i` from config
- `staggered`: to same edge w.p. subnet_p, same pod w.p. pod_p, else anywhere (config: subnet_p, pod_p)
- `inter-pod-incoming`: several pods send to different hosts of one pod via the same core
  switch (paper worst case, (k-1):1)
- `same-id-outgoing`: hosts of one subnet send to hosts elsewhere with the same host ID
  (paper worst case, (k/2):1)
- `all-to-all`, `incast` (M senders -> 1 receiver, M from config), `same-pod`

Flow size classes (traffic mix):
- config `traffic.size_model`: `fixed` | `mice-elephants` | `empirical`
- `mice-elephants`: fraction `elephant_fraction` of flows are elephants (default 10 MB–1 GB),
  rest are mice (default 2–100 KB); sizes configurable
- `empirical`: load a CDF from a CSV in config/ (e.g. a web-search-like distribution)
- every flow carries `class` = mouse | elephant (threshold `elephant_threshold_bytes`)
- `traffic.load` = offered load as a fraction of host link rate

All randomness seeded. Tests: permutation properties (each host sends and receives exactly
once), stride correctness, class fractions within tolerance, determinism per seed.

## Step 3 – Routing schemes, one file each (fattree/routing/)
```
fattree/routing/
  __init__.py         # registry: get_router(name, topo, cfg) -> Router
  base.py             # Router interface
  single_path.py      # one fixed shortest path per destination (OSPF-like baseline)
  ecmp.py             # hash of 5-tuple over equal-cost next hops (industry standard)
  two_level.py        # paper §3.3–3.5, Algorithm 1 and 2, host-ID suffix tables
  flow_classifier.py  # paper §3.6 + §4.2, Algorithm 3
  flow_scheduler.py   # paper §3.7 + §4.3, central scheduler
```
Router interface:
- `route(flow) -> list[node]` (path for a flow)
- `on_failure(failed_links, failed_switches)` (react to failures; behavior per scheme below)
- `stats()` (e.g. scheduler time per placement, memory, number of rearrangements)

Selected by config `routing: single-path | ecmp | two-level | classifier | scheduler`, so I
can toggle schemes in experiments with no code changes.

Scheme details:
- `two_level`: build the real prefix/suffix tables per switch exactly as Algorithms 1 and 2
  (fix the paper's Algorithm 2 typo: destination pods 0..k-1). Upward port at pod switch z
  for host ID i: (i - 2 + z) mod (k/2) + k/2. Unit test against the paper's Fig. 3/Fig. 4
  example (10.0.1.2 -> 10.2.0.3 takes 10.0.1.1 -> 10.0.2.1 -> 10.4.1.1 -> 10.2.2.1 -> 10.2.0.1).
  Failures: static tables do NOT reroute unless config `two_level.failover: true`, which
  implements the §3.8 failure-broadcast behavior (avoid failed links when a choice exists).
- `flow_classifier`: per pod switch; new flow -> least-loaded upward port; every
  `rearrange_interval` (paper t = 1 s) move at most 3 flows from the most- to the
  least-loaded port (Algorithm 3). Flow-level mode: run rearrangement rounds until stable
  or `max_rounds`.
- `flow_scheduler`: edge switches report flows above `elephant_threshold_bytes`; the
  central scheduler keeps link reservation state, linearly searches the (k/2)^2 core paths
  (inter-pod) or k/2 agg paths (intra-pod) for one with no reserved link, reserves it;
  mice use two-level routing. Failed links are marked unavailable. Record time per
  placement and memory of link-state and flow-state (to reproduce paper Table 3).
- Tests for every scheme: valid paths (consecutive nodes adjacent, no failed links when the
  scheme reroutes), determinism, paper example paths.

## Step 4 – Simulation levels (config `simulation.level: graph | flow | packet`)
Keep each engine in its own file under fattree/sim/.

**graph** (existing): structure only. Path metrics must use the edge-switch aggregation
trick (BFS from edge switches on the switch-only graph, count shortest paths, weight by
(k/2)^2 host pairs) so k=48 runs in seconds, not minutes.

**flow** (fattree/sim/flow.py):
- Rates by max-min fairness (progressive filling / water-filling) over link capacities.
- Outputs: per-flow rate, link utilization, aggregate throughput as % of ideal, where
  ideal = sum of host send rates (as in paper §5, 1.536 Gb/s for k=4 at 96 Mb/s).

**packet** (fattree/sim/packet.py), for k = 4 and 8 only (refuse larger k with a clear message):
- Discrete-event simulation with a heapq event queue and a simulated clock.
- Links: capacity, propagation delay; switch output ports: FIFO drop-tail queues with
  `queue_size_packets`.
- Packets: fixed MTU (default 1500 B); flows split into packets.
- Transport: v1 open-loop (UDP-like, constant rate = load x link rate); v2 simple reliable
  transport (fixed window W packets, ACK-clocked, timeout retransmit), selected by
  `packet.transport: udp | window`.
- Path choice per flow from the selected router; optional `packet.spraying: true` picks a
  random equal-cost path per packet (to show reordering).
- Outputs: per-packet delay, queueing delay, drops per link, reordering %, flow completion
  time (FCT) per flow and per class (mouse/elephant), simulated duration.
- Tests: single flow on an empty network has delay = sum of transmission + propagation;
  queue overflow produces drops; spraying produces reordering, per-flow hashing produces none.

## Step 5 – Experiments (config/experiments/*.yaml, one file each)
Each experiment: config file, `python run.py -c config/experiments/<name>.yaml`, results CSV
in results/, figure in figures/, 20–30 trials with 95% CI where random.

1. `table2_reproduction`: flow level, k=4, link rate 96 Mb/s; tree (3.6:1, add an
   oversubscription parameter to traditional-tree) vs two-level vs classifier vs scheduler;
   patterns random, stride(1,2,4,8), staggered(1.0,0.0), (0.5,0.3), (0.2,0.3),
   inter-pod-incoming, same-id-outgoing. Output a table next to the paper's values
   (Tree 53.4/100/78.1/27.9/28.0/100/83.6/64.9/28.0/27.8; Two-level 75.0/100/100/100/100/100/82.0/75.6/50.6/38.5;
   Classifier 76.3/100/100/100/100/100/86.2/80.2/75.1/75.4; Scheduler 93.5/100/99.5/100/99.9/100/93.4/88.5/99.9/87.4)
   and explain differences.
2. `throughput_vs_failures`: flow level, k=8 and 16, link failure 0–30%, one curve per
   routing scheme (two-level without and with failover, ECMP, classifier, scheduler).
3. `collisions_vs_k`: flow level, random permutation, two-level and ECMP, k in allowed_k up
   to 32: throughput lost to hash collisions (paper §5.3 predicts it shrinks with k).
4. `scheduler_cost_vs_k`: time per placement + memory vs k (4..48), compare to paper Table 3.
5. `link_vs_switch_failures`: graph/flow level, same % of links vs switches; connectivity,
   path diversity, throughput; plus edge-switch blast radius (hosts lost per failed switch by layer).
6. `critical_failure_pct_vs_k`: graph level, smallest failure % where host-pair connectivity
   drops below 99.9% (and below 99%), for each k in allowed_k up to 48.
7. `mice_vs_elephants`: packet level, k=4, mice-elephants mix; FCT per class for
   two-level, classifier, scheduler.
8. `reordering_spraying_vs_hashing`: packet level, k=4/8; % reordered packets with
   per-packet spraying vs per-flow hashing (why the paper routes by host ID, §3.5).
9. `incast`: packet level, k=4/8, M = 2..(k^3/4 - 1) senders -> 1 receiver; queue length
   and drops at the receiver's edge port.

Also keep the existing `switches_multifail_vs_k` experiment.

## Step 6 – Visualization (visualize.py)
- Line plots with 95% CI bands (experiments 2, 3, 6), grouped bar charts (1, 5), table
  figure for experiment 1 with paper values side by side, pie charts (link utilization
  buckets; failures by layer; traffic bytes by class), CDF plots of FCT and packet delay
  (7, 9), heatmap of pod x pod throughput or connectivity.
- Consistent colors per routing scheme across all figures; axis labels with units.

## Step 7 – README and CLAUDE.md
- README results section: for every figure, (a) setup, experiment and parameters,
  (b) observed results, explanation and significance (assignment §6). Note the simulation
  level used for each experiment and its limitations (flow-level ignores queues; packet
  level is k <= 8 only; no full TCP).
- Explain simulation levels (graph / flow / packet / emulation) and why the paper used Click
  emulation while this repo simulates.
- Update CLAUDE.md: new modules, config keys, experiments list.
- Keep the AI usage section up to date.

## Config additions (merge into config/default.yaml, keep existing keys)
```yaml
allowed_k: [4, 8, 16, 24, 32, 48, 64]
topology:
  oversubscription: 3.6        # traditional-tree only
simulation:
  level: flow                  # graph | flow | packet
traffic:
  pattern: random
  stride: 1
  subnet_p: 0.5
  pod_p: 0.3
  incast_senders: 8
  load: 1.0
  size_model: mice-elephants   # fixed | mice-elephants | empirical
  elephant_fraction: 0.1
  elephant_threshold_bytes: 10000000
  empirical_cdf: null
routing: two-level             # single-path | ecmp | two-level | classifier | scheduler
two_level:
  failover: false
classifier:
  rearrange_interval_s: 1.0
  max_moves: 3
  max_rounds: 20
packet:
  mtu_bytes: 1500
  queue_size_packets: 100
  propagation_delay_us: 1
  transport: udp               # udp | window
  window_packets: 10
  timeout_ms: 1
  spraying: false
  duration_ms: 100
link_capacity_gbps: 1
```
