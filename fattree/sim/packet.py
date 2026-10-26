"""Packet-level discrete-event simulation (PHASE3_1_EXP8_REORDERING.md Part A),
for k <= 8 only (larger k is refused with a clear message -- Python manages
roughly 1e5-1e6 events/s, and k=4 at 1 Gb/s is already ~8e6 events per
simulated second).

Model: a heapq of `(time_ns, seq, event_type, payload)`; `seq` breaks ties so
equal-time events run in deterministic insertion order. Time is integer
nanoseconds throughout (no float drift). Idle time is free -- the clock jumps
straight to the next event.

Each directed link (u, v) has its own FIFO drop-tail output queue
(`packet.queue_size_packets`) and serializes packets at `link_capacity_gbps`;
after serialization, a packet takes `packet.propagation_delay_ns` to reach v's
queue (store-and-forward -- no cut-through).

Per-flow path: `router.route(flow)` once at flow start, reused for every
packet of that flow (FIFO per-link queues then guarantee in-order arrival).
`packet.spraying: true` instead picks a random equal-cost path (ECMP-style
candidates via nx.all_shortest_paths) independently per DATA packet, which can
reorder packets when their chosen paths see different queueing delay. ACKs
always follow the reverse of their own data packet's path.

Transports (`packet.transport`): see fattree/sim/transport.py for udp and
tcp_lite. Metrics: see fattree/sim/packet_metrics.py.
"""
import heapq
import itertools
import random
from collections import deque

import networkx as nx

from fattree.sim.packet_metrics import FlowStats, PortStats
from fattree.sim.transport import TcpLiteReceiver, TcpLiteSender, UdpSender

MAX_PACKET_LEVEL_K = 8


def check_k_allowed(k, max_k=MAX_PACKET_LEVEL_K):
    if k > max_k:
        raise ValueError(
            f"packet-level simulation only supports k <= {max_k} (got k={k}); "
            "use simulation.level = graph or flow for larger topologies"
        )


class _Packet:
    __slots__ = ("flow_idx", "seq", "size_bytes", "is_ack", "ack_no", "send_time_ns",
                 "path", "hop", "is_retx", "hop_enqueue_time_ns", "queue_delay_ns")

    def __init__(self, flow_idx, seq, size_bytes, is_ack, ack_no, send_time_ns, path, is_retx=False):
        self.flow_idx = flow_idx
        self.seq = seq
        self.size_bytes = size_bytes
        self.is_ack = is_ack
        self.ack_no = ack_no
        self.send_time_ns = send_time_ns
        self.path = path
        self.hop = 0
        self.is_retx = is_retx
        self.hop_enqueue_time_ns = send_time_ns
        self.queue_delay_ns = 0


class PacketSimulator:
    def __init__(self, g, router, flows, cfg):
        k = cfg["topology"]["k"]
        packet_cfg = cfg.get("packet", {})
        max_k = packet_cfg.get("max_k", MAX_PACKET_LEVEL_K)
        check_k_allowed(k, max_k)

        self.g = g
        self.router = router
        self.flows = flows
        self.mtu_bytes = packet_cfg.get("mtu_bytes", 1500)
        self.ack_bytes = packet_cfg.get("ack_bytes", 64)
        self.queue_size = packet_cfg.get("queue_size_packets", 100)
        self.prop_delay_ns = packet_cfg.get("propagation_delay_ns", 1000)
        self.transport_name = packet_cfg.get("transport", "tcp_lite")
        self.init_cwnd = packet_cfg.get("init_cwnd", 10)
        self.dupack_threshold = packet_cfg.get("dupack_threshold", 3)
        self.min_rto_ns = int(packet_cfg.get("min_rto_ms", 200) * 1e6)
        max_rto_ms = packet_cfg.get("max_rto_ms") or 60 * packet_cfg.get("min_rto_ms", 200)
        self.max_rto_ns = int(max_rto_ms * 1e6)
        self.spraying = packet_cfg.get("spraying", False)
        self.duration_ns = int(packet_cfg.get("duration_ms", 100) * 1e6)
        self.capacity_bps = cfg["link_capacity_gbps"] * 1e9
        self.load = cfg["traffic"]["load"]

        self._events = []
        self._tiebreak = itertools.count()
        self.rng = random.Random(cfg.get("seed", 1))

        self._queue_store = {}  # (u, v) -> collections.deque
        self.link_free_at_ns = {}
        self.port_stats = {}    # (u, v) -> PortStats

        self.flow_paths = {}    # flow_idx -> list[path] candidates (len 1 unless spraying)
        self.flow_stats = {}    # flow_idx -> FlowStats
        self.senders = {}       # flow_idx -> TcpLiteSender (tcp_lite only)
        self.receivers = {}     # flow_idx -> TcpLiteReceiver (tcp_lite only)
        self.flow_n_packets = {}
        self.flow_dropped = set()
        self.delivered_seqs = {}  # flow_idx -> set of seq delivered at least once (udp finish detection)

        self.events_processed = 0
        self.total_sent = 0
        self.total_delivered = 0
        self.total_dropped = 0
        self.packet_delays_ns = []
        self.packet_queue_delays_ns = []
        self.last_time_ns = 0

        self._build()

    # --- setup --------------------------------------------------------------------

    def _queue(self, key):
        q = self._queue_store.get(key)
        if q is None:
            q = deque()
            self._queue_store[key] = q
            self.port_stats[key] = PortStats()
        return q

    def _candidate_paths(self, flow):
        if self.spraying:
            try:
                return list(nx.all_shortest_paths(self.g, flow.src, flow.dst))
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                return []
        path = self.router.route(flow)
        return [path] if path else []

    def _packet_path(self, candidates):
        return self.rng.choice(candidates) if self.spraying else candidates[0]

    def _build(self):
        for idx, flow in enumerate(self.flows):
            candidates = self._candidate_paths(flow)
            start_ns = int(flow.start_time * 1e9)
            self.flow_stats[idx] = FlowStats(idx, flow.cls, flow.size_bytes, start_ns)
            if not candidates:
                self.flow_dropped.add(idx)
                self.flow_n_packets[idx] = 0
                continue
            self.flow_paths[idx] = candidates
            n_packets = max(1, -(-int(flow.size_bytes) // self.mtu_bytes))
            self.flow_n_packets[idx] = n_packets
            self.delivered_seqs[idx] = set()

            if self.transport_name == "udp":
                rate_bps = max(self.load * self.capacity_bps, 1.0)
                sender = UdpSender(n_packets, rate_bps, self.mtu_bytes, flow.size_bytes)
                for seq, t in enumerate(sender.send_times_ns()):
                    self._schedule(start_ns + t, "send", (idx, seq))
            elif self.transport_name == "tcp_lite":
                self.senders[idx] = TcpLiteSender(n_packets, self.min_rto_ns, self.init_cwnd,
                                                   self.dupack_threshold, self.max_rto_ns)
                self.receivers[idx] = TcpLiteReceiver()
                self._schedule(start_ns, "flow_start", (idx,))
            else:
                raise ValueError(f"unknown packet.transport: {self.transport_name!r}")

    def _schedule(self, time_ns, kind, payload):
        heapq.heappush(self._events, (time_ns, next(self._tiebreak), kind, payload))

    # --- event loop --------------------------------------------------------------------

    def run(self):
        while self._events:
            time_ns, _, kind, payload = heapq.heappop(self._events)
            if time_ns > self.duration_ns:
                break
            if time_ns < self.last_time_ns:
                raise AssertionError(
                    f"event time went backwards: {kind} at {time_ns}ns after {self.last_time_ns}ns "
                    f"-- time travel must be impossible, not silently tolerated (see PHASE7.md)"
                )
            self.last_time_ns = time_ns
            self.events_processed += 1
            if kind == "send":
                self._handle_send(time_ns, payload)
            elif kind == "flow_start":
                self._handle_flow_start(time_ns, payload)
            elif kind == "tx_done":
                self._handle_tx_done(time_ns, payload)
            elif kind == "arrive":
                self._handle_arrive(time_ns, payload)
            elif kind == "timeout":
                self._handle_timeout(time_ns, payload)
        return self.results()

    def _size_for_seq(self, flow, seq, n_packets):
        return min(self.mtu_bytes, flow.size_bytes - seq * self.mtu_bytes)

    def _send_data_packet(self, idx, seq, now_ns):
        flow = self.flows[idx]
        n_packets = self.flow_n_packets[idx]
        if not (0 <= seq < n_packets):
            raise AssertionError(f"flow {idx}: refusing to send seq {seq}, outside [0, {n_packets})")
        size = self._size_for_seq(flow, seq, n_packets)
        if size <= 0:
            raise AssertionError(f"flow {idx}: computed size {size} <= 0 for seq {seq} (n_packets={n_packets})")
        path = self._packet_path(self.flow_paths[idx])
        is_retx = False
        if idx in self.senders:
            is_retx = self.senders[idx].on_send(seq, now_ns)
            if is_retx:
                self.flow_stats[idx].retransmit_count += 1
        pkt = _Packet(idx, seq, size, is_ack=False, ack_no=None, send_time_ns=now_ns, path=path, is_retx=is_retx)
        self.total_sent += 1
        self._enqueue_departure(pkt, now_ns)

    def _handle_send(self, now_ns, payload):
        """UDP data packet send."""
        idx, seq = payload
        self._send_data_packet(idx, seq, now_ns)

    def _handle_flow_start(self, now_ns, payload):
        (idx,) = payload
        sender = self.senders[idx]
        seq = sender.next_send_seq()
        sent_any = False
        while seq is not None:
            self._send_data_packet(idx, seq, now_ns)
            sent_any = True
            seq = sender.next_send_seq()
        if sent_any:
            self._arm_timer(idx, now_ns)

    def _arm_timer(self, idx, now_ns):
        sender = self.senders[idx]
        if sender.has_outstanding_data():
            gen = sender.timer_generation
            self._schedule(now_ns + sender.rto_ns, "timeout", (idx, gen))

    def _enqueue_departure(self, pkt, now_ns):
        u, v = pkt.path[pkt.hop], pkt.path[pkt.hop + 1]
        self._enqueue(now_ns, u, v, pkt)

    def _enqueue(self, now_ns, u, v, pkt):
        key = (u, v)
        q = self._queue(key)
        if len(q) >= self.queue_size:
            self.port_stats[key].record_drop(now_ns)
            # total_sent/delivered/dropped are a data-packet-only conservation
            # invariant (ACK drops are still counted per-port above, just not
            # folded into this data-only total).
            if not pkt.is_ack:
                self.total_dropped += 1
            return
        was_empty = len(q) == 0
        pkt.hop_enqueue_time_ns = now_ns
        q.append(pkt)
        self.port_stats[key].record_queue_len(now_ns, len(q))
        if was_empty:
            # Always kick off transmission when the queue was empty, even if
            # link_free_at_ns[key] is still in the future (_start_tx's own
            # start = max(now, link_free_at) already handles that case
            # correctly). The old `and link_free_at_ns.get(key, 0) <= now_ns`
            # guard assumed a transmission must already be scheduled whenever
            # the link looked "busy" -- true unless that busy time came from
            # a since-sent packet with a corrupt (e.g. negative-size, from
            # PHASE7.md's ghost-packet bug) transmission time computed
            # *before* the current time, in which case nothing was actually
            # scheduled and this port would silently never transmit again.
            self._start_tx(key, now_ns)

    def _start_tx(self, key, now_ns):
        q = self._queue_store[key]
        if not q:
            return
        pkt = q[0]
        start = max(now_ns, self.link_free_at_ns.get(key, 0))
        pkt.queue_delay_ns += start - pkt.hop_enqueue_time_ns
        tx_time_ns = int(pkt.size_bytes * 8 * 1e9 / self.capacity_bps)
        if tx_time_ns <= 0:
            raise AssertionError(f"link {key}: non-positive transmission time {tx_time_ns}ns for a {pkt.size_bytes}-byte packet")
        done = start + tx_time_ns
        self.link_free_at_ns[key] = done
        self._schedule(done, "tx_done", key)

    def _handle_tx_done(self, now_ns, key):
        q = self._queue_store[key]
        pkt = q.popleft()
        self.port_stats[key].record_queue_len(now_ns, len(q))
        u, v = key
        self._schedule(now_ns + self.prop_delay_ns, "arrive", (v, pkt))
        if q:
            self._start_tx(key, now_ns)

    def _handle_arrive(self, now_ns, payload):
        v, pkt = payload
        pkt.hop += 1
        if v == pkt.path[-1]:
            self._deliver(now_ns, pkt)
            return
        self._enqueue_departure(pkt, now_ns)

    def _deliver(self, now_ns, pkt):
        if pkt.is_ack:
            self._handle_ack_delivered(now_ns, pkt)
            return

        fs = self.flow_stats[pkt.flow_idx]
        fs.record_arrival(pkt.seq)
        self.total_delivered += 1
        self.packet_delays_ns.append(now_ns - pkt.send_time_ns)
        self.packet_queue_delays_ns.append(pkt.queue_delay_ns)

        if pkt.flow_idx in self.receivers:
            receiver = self.receivers[pkt.flow_idx]
            ack_no, is_dup_delivery = receiver.on_data(pkt.seq)
            if is_dup_delivery:
                fs.spurious_retransmit_count += 1
            ack_path = list(reversed(pkt.path))  # pkt.hop == len(pkt.path)-1 at delivery
            ack = _Packet(pkt.flow_idx, pkt.seq, self.ack_bytes, is_ack=True, ack_no=ack_no,
                          send_time_ns=now_ns, path=ack_path)
            self._enqueue_departure(ack, now_ns)
        else:
            delivered = self.delivered_seqs[pkt.flow_idx]
            delivered.add(pkt.seq)
            if len(delivered) == self.flow_n_packets[pkt.flow_idx]:
                fs.finish(now_ns)

    def _handle_ack_delivered(self, now_ns, pkt):
        idx = pkt.flow_idx
        sender = self.senders[idx]
        fs = self.flow_stats[idx]
        result = sender.on_ack(pkt.ack_no, now_ns)

        if result == "new":
            if sender.done and fs.finish_time_ns is None:
                fs.finish(sender.finish_time_ns)
            seq = sender.next_send_seq()
            while seq is not None:
                self._send_data_packet(idx, seq, now_ns)
                seq = sender.next_send_seq()
            self._arm_timer(idx, now_ns)
        elif result == "fast_retransmit":
            fs.fast_retransmit_count += 1
            self._send_data_packet(idx, sender.send_base, now_ns)
            self._arm_timer(idx, now_ns)

    def _handle_timeout(self, now_ns, payload):
        """Go-back-N recovery (RFC 5681): on_timeout() rewinds the sender's
        next_seq to send_base, so draining next_send_seq() here resends the
        whole unacked window as cwnd reopens -- one timeout recovers a whole
        burst of losses, not one timeout per lost packet."""
        idx, generation = payload
        sender = self.senders[idx]
        fs = self.flow_stats[idx]
        if not sender.on_timeout(generation, now_ns):
            return  # stale
        fs.timeout_count += 1
        seq = sender.next_send_seq()
        while seq is not None:
            self._send_data_packet(idx, seq, now_ns)
            seq = sender.next_send_seq()
        self._arm_timer(idx, now_ns)

    # --- results --------------------------------------------------------------------

    def results(self):
        fct_ns_by_class = {"mouse": [], "elephant": []}
        unfinished_by_class = {"mouse": 0, "elephant": 0}
        for idx, fs in self.flow_stats.items():
            if idx in self.flow_dropped:
                continue
            if fs.fct_ns() is not None:
                fct_ns_by_class.setdefault(fs.cls, []).append(fs.fct_ns())
            else:
                unfinished_by_class[fs.cls] = unfinished_by_class.get(fs.cls, 0) + 1

        return {
            "events_processed": self.events_processed,
            "total_sent": self.total_sent,
            "total_delivered": self.total_delivered,
            "total_dropped": self.total_dropped,
            "dropped_flows": sorted(self.flow_dropped),
            "flow_stats": self.flow_stats,
            "port_stats": self.port_stats,
            "fct_ns_by_class": fct_ns_by_class,
            "unfinished_by_class": unfinished_by_class,
            "packet_delays_ns": self.packet_delays_ns,
            "packet_queue_delays_ns": self.packet_queue_delays_ns,
            "avg_packet_delay_ns": (sum(self.packet_delays_ns) / len(self.packet_delays_ns)
                                     if self.packet_delays_ns else None),
            "avg_queueing_delay_ns": (sum(self.packet_queue_delays_ns) / len(self.packet_queue_delays_ns)
                                       if self.packet_queue_delays_ns else None),
        }


def simulate_packets(g, router, flows, cfg):
    return PacketSimulator(g, router, flows, cfg).run()


def ideal_fct_ns(g, router, flow, cfg):
    """FCT for `flow` alone on an empty network (no contention) -- the
    "ideal" baseline for slowdown = FCT / ideal_fct."""
    solo = PacketSimulator(g, router, [flow], cfg)
    result = solo.run()
    fs = result["flow_stats"][0]
    return fs.fct_ns()
