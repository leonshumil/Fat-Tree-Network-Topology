"""Packet-level discrete-event simulation (PHASE2_PROMPT.md Step 4), for k = 4 or
8 only (larger k is refused with a clear message -- this engine is for detailed
micro-behavior studies, not scale).

Model: a heapq event queue and a simulated clock. Each directed link (u, v) has
a FIFO drop-tail output queue (`packet.queue_size_packets`) and serializes
packets at `link_capacity_gbps`; after serialization, a packet takes
`packet.propagation_delay_us` to reach v's queue (simple store-and-forward --
no cut-through). Flows are split into `packet.mtu_bytes` packets.

Transport (`packet.transport`):
  udp    - open-loop, constant send rate = traffic.load * link_capacity (v1).
  window - a fixed window of `packet.window_packets` outstanding packets,
           ACK-clocked (ACKs travel the reverse path through the same model),
           `packet.timeout_ms` timeout triggers retransmission (v2). This is a
           simple reliable transport, not full TCP (no congestion control).

Per-flow path: router.route(flow) once, reused for every packet of that flow
(FIFO per-link queues then guarantee in-order arrival -- no reordering).
`packet.spraying: true` instead picks a random equal-cost path per PACKET
(ECMP-style candidates via nx.all_shortest_paths), which can reorder packets
when their chosen paths see different queueing delay.

Outputs: per-packet delay (+ queueing delay), drops per link, reordering % per
flow, flow completion time (FCT) overall and per class, simulated duration.
"""
import heapq
import itertools
import random
from collections import deque

import networkx as nx

MAX_PACKET_LEVEL_K = 8


def check_k_allowed(k):
    if k > MAX_PACKET_LEVEL_K:
        raise ValueError(
            f"packet-level simulation only supports k <= {MAX_PACKET_LEVEL_K} (got k={k}); "
            "use simulation.level = graph or flow for larger topologies"
        )


class _Packet:
    __slots__ = ("flow_idx", "seq", "size_bytes", "send_time", "is_ack", "path", "hop",
                 "hop_enqueue_time", "queue_delay_total")

    def __init__(self, flow_idx, seq, size_bytes, send_time, is_ack, path):
        self.flow_idx = flow_idx
        self.seq = seq
        self.size_bytes = size_bytes
        self.send_time = send_time
        self.is_ack = is_ack
        self.path = path
        self.hop = 0  # index into path of the node this packet is currently departing
        self.hop_enqueue_time = send_time
        self.queue_delay_total = 0.0


class PacketSimulator:
    def __init__(self, g, router, flows, cfg):
        k = cfg["topology"]["k"]
        check_k_allowed(k)

        self.g = g
        self.router = router
        self.flows = flows
        packet_cfg = cfg.get("packet", {})
        self.mtu_bytes = packet_cfg.get("mtu_bytes", 1500)
        self.queue_size = packet_cfg.get("queue_size_packets", 100)
        self.prop_delay = packet_cfg.get("propagation_delay_us", 1) * 1e-6
        self.transport = packet_cfg.get("transport", "udp")
        self.window = packet_cfg.get("window_packets", 10)
        self.timeout = packet_cfg.get("timeout_ms", 1) * 1e-3
        self.spraying = packet_cfg.get("spraying", False)
        self.duration = packet_cfg.get("duration_ms", 100) * 1e-3
        self.capacity_bps = cfg["link_capacity_gbps"] * 1e9
        self.load = cfg["traffic"]["load"]

        self._events = []
        self._tiebreak = itertools.count()
        self.rng = random.Random(cfg.get("seed", 1))

        self.queues = {}            # (u, v) -> deque[_Packet]
        self.link_free_at = {}      # (u, v) -> time the link is next free

        self.packet_delays = []
        self.queueing_delays = []
        self.drops_per_link = {}
        self.reordered_packets = 0
        self.delivered_packets = 0
        self.flow_paths = {}        # flow_idx -> path (per-flow hashing) or None (spraying)
        self.flow_last_delivered_seq = {}
        self.flow_sent_count = {}
        self.flow_acked_count = {}
        self.flow_completion_time = {}
        self.flow_n_packets = {}
        self.flow_dropped = set()

        self._build_schedule()

    # --- setup --------------------------------------------------------------------

    def _candidate_paths(self, flow):
        if self.spraying:
            try:
                return list(nx.all_shortest_paths(self.g, flow.src, flow.dst))
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                return []
        path = self.router.route(flow)
        return [path] if path else []

    def _packet_path(self, flow_idx, candidates):
        return self.rng.choice(candidates) if self.spraying else candidates[0]

    def _build_schedule(self):
        for idx, flow in enumerate(self.flows):
            candidates = self._candidate_paths(flow)
            if not candidates:
                self.flow_dropped.add(idx)
                self.flow_n_packets[idx] = 0
                continue
            self.flow_paths[idx] = candidates
            n_packets = max(1, -(-int(flow.size_bytes) // self.mtu_bytes))
            self.flow_n_packets[idx] = n_packets
            self.flow_sent_count[idx] = 0
            self.flow_acked_count[idx] = 0
            self.flow_last_delivered_seq[idx] = -1

            if self.transport == "udp":
                # Cumulative clock (not seq * gap): the last packet of a flow is
                # usually smaller than mtu_bytes, so its own transmission time
                # cannot be used as every prior packet's spacing -- that bug sent
                # the final packet far too early and broke in-order delivery.
                rate_bps = max(self.load * self.capacity_bps, 1.0)
                t = flow.start_time
                for seq in range(n_packets):
                    size = min(self.mtu_bytes, flow.size_bytes - seq * self.mtu_bytes)
                    self._schedule(t, "send", (idx, seq, size))
                    t += (size * 8) / rate_bps
            elif self.transport == "window":
                for seq in range(min(self.window, n_packets)):
                    size = min(self.mtu_bytes, flow.size_bytes - seq * self.mtu_bytes)
                    self._schedule(flow.start_time, "send", (idx, seq, size))
            else:
                raise ValueError(f"unknown packet.transport: {self.transport!r}")

    def _schedule(self, time, kind, payload):
        heapq.heappush(self._events, (time, next(self._tiebreak), kind, payload))

    # --- event loop --------------------------------------------------------------------

    def run(self):
        while self._events:
            time, _, kind, payload = heapq.heappop(self._events)
            if time > self.duration:
                break
            if kind == "send":
                self._handle_send(time, payload)
            elif kind == "tx_done":
                self._handle_tx_done(time, payload)
            elif kind == "arrive":
                self._handle_arrive(time, payload)
            elif kind == "timeout":
                self._handle_timeout(time, payload)
        return self._results()

    def _handle_send(self, time, payload):
        flow_idx, seq, size = payload
        candidates = self.flow_paths[flow_idx]
        path = self._packet_path(flow_idx, candidates)
        pkt = _Packet(flow_idx, seq, size, time, is_ack=False, path=path)
        self.flow_sent_count[flow_idx] += 1
        if self.transport == "window":
            self._schedule(time + self.timeout, "timeout", (flow_idx, seq, pkt))
        self._enqueue_departure(pkt, time)

    def _enqueue_departure(self, pkt, time):
        u, v = pkt.path[pkt.hop], pkt.path[pkt.hop + 1]
        self._handle_enqueue(time, (u, v, pkt))

    def _handle_enqueue(self, time, payload):
        u, v, pkt = payload
        q = self.queues.setdefault((u, v), deque())
        if len(q) >= self.queue_size:
            self.drops_per_link[(u, v)] = self.drops_per_link.get((u, v), 0) + 1
            return
        was_empty = len(q) == 0
        pkt.hop_enqueue_time = time
        q.append(pkt)
        if was_empty and self.link_free_at.get((u, v), 0.0) <= time:
            self._start_tx(u, v, time)

    def _start_tx(self, u, v, time):
        q = self.queues[(u, v)]
        if not q:
            return
        pkt = q[0]
        start = max(time, self.link_free_at.get((u, v), 0.0))
        pkt.queue_delay_total += start - pkt.hop_enqueue_time
        tx_time = (pkt.size_bytes * 8) / self.capacity_bps
        done = start + tx_time
        self.link_free_at[(u, v)] = done
        self._schedule(done, "tx_done", (u, v))

    def _handle_tx_done(self, time, payload):
        u, v = payload
        q = self.queues[(u, v)]
        pkt = q.popleft()
        self._schedule(time + self.prop_delay, "arrive", (v, pkt))
        if q:
            self._start_tx(u, v, time)

    def _handle_arrive(self, time, payload):
        v, pkt = payload
        pkt.hop += 1
        if v == pkt.path[-1]:
            self._deliver(time, pkt)
            return
        self._enqueue_departure(pkt, time)

    def _deliver(self, time, pkt):
        if pkt.is_ack:
            self._handle_ack_delivered(time, pkt)
            return

        delay = time - pkt.send_time
        self.packet_delays.append(delay)
        self.queueing_delays.append(pkt.queue_delay_total)
        self.delivered_packets += 1

        flow_idx = pkt.flow_idx
        if pkt.seq < self.flow_last_delivered_seq[flow_idx]:
            self.reordered_packets += 1
        self.flow_last_delivered_seq[flow_idx] = max(self.flow_last_delivered_seq[flow_idx], pkt.seq)

        if self.transport == "window":
            ack_path = list(reversed(pkt.path))
            ack = _Packet(flow_idx, pkt.seq, 64, time, is_ack=True, path=ack_path)
            self._enqueue_departure(ack, time)
        else:
            self._maybe_complete_flow(flow_idx, time, pkt.seq)

    def _handle_ack_delivered(self, time, pkt):
        flow_idx = pkt.flow_idx
        self.flow_acked_count[flow_idx] += 1
        self._maybe_complete_flow(flow_idx, time, pkt.seq)

        # next_seq is read, not incremented, here: _handle_send() is the sole
        # place that increments flow_sent_count, once per "send" event it
        # processes. Incrementing it here too (as an earlier version did) made
        # it jump by 2 per ACK-triggered send and silently skipped a sequence
        # number.
        n_packets = self.flow_n_packets[flow_idx]
        next_seq = self.flow_sent_count[flow_idx]
        if next_seq < n_packets:
            flow = self.flows[flow_idx]
            size = min(self.mtu_bytes, flow.size_bytes - next_seq * self.mtu_bytes)
            self._schedule(time, "send", (flow_idx, next_seq, size))

    def _maybe_complete_flow(self, flow_idx, time, seq):
        n_packets = self.flow_n_packets[flow_idx]
        if self.transport == "window":
            if self.flow_acked_count[flow_idx] >= n_packets:
                self.flow_completion_time.setdefault(flow_idx, time)
        else:
            if seq == n_packets - 1:
                self.flow_completion_time.setdefault(flow_idx, time)

    def _handle_timeout(self, time, payload):
        flow_idx, seq, pkt = payload
        if seq <= self.flow_last_delivered_seq.get(flow_idx, -1):
            return  # already delivered (and acked), nothing to retransmit
        # retransmit: re-enqueue a fresh copy on the same path, reset its timer
        flow = self.flows[flow_idx]
        size = min(self.mtu_bytes, flow.size_bytes - seq * self.mtu_bytes)
        new_pkt = _Packet(flow_idx, seq, size, time, is_ack=False, path=pkt.path)
        self._schedule(time + self.timeout, "timeout", (flow_idx, seq, new_pkt))
        self._enqueue_departure(new_pkt, time)

    def _results(self):
        total_drops = sum(self.drops_per_link.values())
        fct_by_class = {"mouse": [], "elephant": []}
        for idx, flow in enumerate(self.flows):
            if idx in self.flow_completion_time:
                fct = self.flow_completion_time[idx] - flow.start_time
                fct_by_class.setdefault(flow.cls, []).append(fct)

        return {
            "simulated_duration_s": self.duration,
            "delivered_packets": self.delivered_packets,
            "dropped_packets": total_drops,
            "drops_per_link": dict(self.drops_per_link),
            "reordered_packets": self.reordered_packets,
            "reorder_pct": (100 * self.reordered_packets / self.delivered_packets) if self.delivered_packets else 0.0,
            "avg_packet_delay_s": (sum(self.packet_delays) / len(self.packet_delays)) if self.packet_delays else None,
            "avg_queueing_delay_s": (
                (sum(self.queueing_delays) / len(self.queueing_delays)) if self.queueing_delays else None
            ),
            "dropped_flows": sorted(self.flow_dropped),
            "flow_completion_times_s": dict(self.flow_completion_time),
            "fct_by_class_s": fct_by_class,
        }


def simulate_packets(g, router, flows, cfg):
    return PacketSimulator(g, router, flows, cfg).run()
