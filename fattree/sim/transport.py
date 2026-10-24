"""Transport protocols for the packet-level simulator (PHASE3_1 Part A).

udp: open-loop, constant send rate = load * link_capacity_gbps; no ACKs, no
retransmission. Isolates pure network effects from transport effects.

tcp_lite: a simplified Reno-like reliable transport --
  - cwnd in packets (float): slow start (cwnd += newly-acked packets) until
    ssthresh, then congestion avoidance (cwnd += newly-acked/cwnd, i.e. ~+1
    packet per RTT).
  - Cumulative ACKs only (no SACK); the receiver buffers out-of-order packets
    and ACKs the next expected sequence number.
  - `dupack_threshold` duplicate ACKs (ack_no == send_base repeated) trigger
    fast retransmit + cwnd/ssthresh halved (simplified fast recovery: cwnd is
    set to the new ssthresh, not inflated further during recovery).
    `dupack_threshold=None` disables fast retransmit entirely.
  - RTO per RFC 6298 (Jacobson/Karels): SRTT/RTTVAR-based, exponential backoff
    on timeout, cwnd reset to 1 packet. RTT is sampled only from segments that
    were never retransmitted (Karn's algorithm) -- a stale timeout event (from
    a since-superseded RTO generation) is detected via `timer_generation` and
    ignored by the caller.

NOT implemented (documented limitation, see README): SACK, delayed ACKs, ECN,
slow-start-after-idle, full NewReno partial-ACK handling during fast recovery.
"""


class UdpSender:
    def __init__(self, n_packets, rate_bps, mtu_bytes, flow_size_bytes):
        self.n_packets = n_packets
        self.rate_bps = rate_bps
        self.mtu_bytes = mtu_bytes
        self.flow_size_bytes = flow_size_bytes

    def packet_size(self, seq):
        return min(self.mtu_bytes, self.flow_size_bytes - seq * self.mtu_bytes)

    def send_times_ns(self):
        """Cumulative clock, not seq * gap: a smaller last packet must not be
        used as every prior packet's spacing (see fattree/sim/flow.py's UDP
        pacing bug, fixed in Phase 2, for why this matters)."""
        t = 0
        times = []
        for seq in range(self.n_packets):
            times.append(t)
            gap_ns = int(self.packet_size(seq) * 8 * 1e9 / self.rate_bps)
            t += gap_ns
        return times


class TcpLiteSender:
    """Per-flow Reno-lite sender state machine (time in integer ns)."""

    def __init__(self, n_packets, min_rto_ns, init_cwnd=10, dupack_threshold=3, max_rto_ns=None):
        self.n_packets = n_packets
        self.min_rto_ns = min_rto_ns
        self.max_rto_ns = max_rto_ns if max_rto_ns is not None else min_rto_ns * 60
        self.cwnd = float(init_cwnd)
        self.ssthresh = float(1 << 30)
        self.send_base = 0
        self.next_seq = 0
        self.dup_ack_count = 0
        self.dupack_threshold = dupack_threshold
        self.srtt = None
        self.rttvar = None
        self.rto_ns = min_rto_ns
        self.timer_generation = 0
        self.send_time = {}         # seq -> most recent send_time_ns
        self.retransmitted = set()  # seqs ever retransmitted (Karn's algorithm)
        self.retransmit_count = 0
        self.timeout_count = 0
        self.fast_retransmit_count = 0
        self.done = False
        self.finish_time_ns = None

    def has_outstanding_data(self):
        return self.send_base < self.next_seq

    def next_send_seq(self):
        """Return the next new seq to send if the window allows it, else None."""
        if self.next_seq < self.n_packets and (self.next_seq - self.send_base) < int(self.cwnd):
            seq = self.next_seq
            self.next_seq += 1
            return seq
        return None

    def on_send(self, seq, now_ns, is_retx):
        self.send_time[seq] = now_ns
        if is_retx:
            self.retransmitted.add(seq)
            self.retransmit_count += 1

    def _update_rto(self, rtt_sample_ns):
        if self.srtt is None:
            self.srtt = float(rtt_sample_ns)
            self.rttvar = rtt_sample_ns / 2.0
        else:
            alpha, beta = 1 / 8, 1 / 4
            self.rttvar = (1 - beta) * self.rttvar + beta * abs(self.srtt - rtt_sample_ns)
            self.srtt = (1 - alpha) * self.srtt + alpha * rtt_sample_ns
        self.rto_ns = min(self.max_rto_ns, max(self.min_rto_ns, int(self.srtt + 4 * self.rttvar)))

    def on_ack(self, ack_no, now_ns):
        """Returns 'new' | 'dup' | 'fast_retransmit' | 'stale'."""
        if ack_no == self.send_base:
            self.dup_ack_count += 1
            if self.dupack_threshold is not None and self.dup_ack_count == self.dupack_threshold:
                self.ssthresh = max(self.cwnd / 2, 2.0)
                self.cwnd = self.ssthresh
                self.fast_retransmit_count += 1
                return "fast_retransmit"
            return "dup"
        if ack_no > self.send_base:
            newly_acked = ack_no - self.send_base
            if self.cwnd < self.ssthresh:
                self.cwnd += newly_acked
            else:
                self.cwnd += newly_acked / self.cwnd
            last_acked_seq = ack_no - 1
            if last_acked_seq in self.send_time and last_acked_seq not in self.retransmitted:
                self._update_rto(now_ns - self.send_time[last_acked_seq])
            self.send_base = ack_no
            self.dup_ack_count = 0
            self.timer_generation += 1  # invalidate any in-flight timer for the old window
            if self.send_base >= self.n_packets:
                self.done = True
                self.finish_time_ns = now_ns
            return "new"
        return "stale"

    def on_timeout(self, generation, now_ns):
        """Returns True if this was a live (non-stale) timeout that was acted on."""
        if generation != self.timer_generation:
            return False
        self.ssthresh = max(self.cwnd / 2, 2.0)
        self.cwnd = 1.0
        self.rto_ns = min(self.max_rto_ns, self.rto_ns * 2)
        self.timeout_count += 1
        self.timer_generation += 1
        return True


class TcpLiteReceiver:
    """Cumulative-ACK receiver: buffers out-of-order packets, ACKs the next
    expected (in-order) sequence number."""

    def __init__(self):
        self.expected_seq = 0
        self.received = set()
        self.delivery_count = {}  # seq -> number of times this seq's data has arrived

    def on_data(self, seq):
        """Record one arrival of `seq`'s data; returns (ack_no, is_duplicate_delivery)."""
        self.delivery_count[seq] = self.delivery_count.get(seq, 0) + 1
        is_duplicate = seq in self.received
        self.received.add(seq)
        while self.expected_seq in self.received:
            self.expected_seq += 1
        return self.expected_seq, is_duplicate
