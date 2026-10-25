"""Per-packet / per-flow / per-port statistics for the packet-level simulator
(PHASE3_1 Part A).

Reordering follows RFC 4737's definition: a data packet is reordered if it
arrives with a sequence number lower than the highest sequence number already
*arrived* for that flow (not "already delivered in order" -- a packet sitting
in the receiver's out-of-order buffer still counts as "arrived"). Reorder
extent is how many higher-seq packets arrived before it.
"""


class FlowStats:
    def __init__(self, flow_idx, cls, size_bytes, start_time_ns):
        self.flow_idx = flow_idx
        self.cls = cls
        self.size_bytes = size_bytes
        self.start_time_ns = start_time_ns
        self.finish_time_ns = None
        self.unfinished = True

        self.highest_seq_arrived = -1
        self.reordered_packets = 0
        self.reorder_extents = []

        self.retransmit_count = 0
        self.timeout_count = 0
        self.fast_retransmit_count = 0
        self.spurious_retransmit_count = 0

    def record_arrival(self, seq):
        """Call once per data-packet arrival at its destination; updates
        reordering stats. Returns True if this arrival was reordered."""
        if seq < self.highest_seq_arrived:
            self.reordered_packets += 1
            self.reorder_extents.append(self.highest_seq_arrived - seq)
            return True
        self.highest_seq_arrived = max(self.highest_seq_arrived, seq)
        return False

    def finish(self, time_ns):
        self.finish_time_ns = time_ns
        self.unfinished = False

    def fct_ns(self):
        if self.finish_time_ns is None:
            return None
        return self.finish_time_ns - self.start_time_ns

    def goodput_bps(self):
        fct = self.fct_ns()
        if not fct:
            return None
        return self.size_bytes * 8 * 1e9 / fct


class PortStats:
    def __init__(self):
        self.drops = 0
        self.queue_len_samples = []  # (time_ns, length) pairs, sampled on enqueue/dequeue
        self.drop_times_ns = []      # time of each queue-overflow drop at this port

    def record_queue_len(self, time_ns, length):
        self.queue_len_samples.append((time_ns, length))

    def record_drop(self, time_ns):
        self.drops += 1
        self.drop_times_ns.append(time_ns)

    def max_queue_len(self):
        return max((n for _, n in self.queue_len_samples), default=0)

    def mean_queue_len(self):
        if not self.queue_len_samples:
            return 0.0
        return sum(n for _, n in self.queue_len_samples) / len(self.queue_len_samples)


def slowdown(flow_stats, ideal_fct_ns):
    fct = flow_stats.fct_ns()
    if fct is None or not ideal_fct_ns:
        return None
    return fct / ideal_fct_ns


def mean(values):
    values = list(values)
    return sum(values) / len(values) if values else None


def percentile(values, p):
    values = sorted(values)
    if not values:
        return None
    idx = min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))
    return values[idx]
