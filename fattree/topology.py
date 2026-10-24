"""Build a k-ary fat-tree topology per Al-Fares, Loukissas, Vahdat, SIGCOMM 2008 (Sec. 3).

Three layers: core, aggregation ("agg"), edge, plus hosts. Addressing follows
the paper's Sec. 3.2 scheme:
  - pod switch:  10.pod.switch.1   (switch 0..k/2-1 = edge, k/2..k-1 = agg)
  - core switch: 10.k.j.i          (j, i in 1..k/2)
  - host:        10.pod.switch.ID  (ID in 2..k/2+1)
Agg switch a (0-based within its pod) connects to core row j = a + 1
(Sec. 3.2 / Fig. 3).
"""
import networkx as nx


def validate_k(k):
    if not isinstance(k, int) or isinstance(k, bool) or k < 4 or k % 2 != 0:
        raise ValueError(f"k must be an even integer >= 4, got {k!r}")


def hosts_of(g):
    """Return host node names, sorted by (pod, switch, host_id) for a deterministic
    order -- used by traffic.py's patterns (e.g. stride, index-based derangement)."""
    hosts = [n for n, d in g.nodes(data=True) if d["type"] == "host"]
    hosts.sort(key=lambda n: (g.nodes[n]["pod"], g.nodes[n]["switch"], g.nodes[n]["host_id"]))
    return hosts


def pod_switch_name(pod, switch, half):
    """Node name for a pod switch, following build_fat_tree()'s naming convention
    (switch < half -> edge, else agg). Used by fattree/routing to resolve computed
    port numbers back to node names without re-deriving the naming scheme."""
    kind = "edge" if switch < half else "agg"
    return f"{kind}-p{pod}-s{switch}"


def core_switch_name(j, i):
    """Node name for core switch (j, i), 1-indexed as in the paper's addressing."""
    return f"core-{j}-{i}"


def build_fat_tree(k):
    """Return a networkx.Graph for a k-ary fat-tree.

    Node attribute 'type' is one of 'core', 'agg', 'edge', 'host'.
    Edge attribute 'layer' is one of 'host-edge', 'edge-agg', 'agg-core'.
    """
    validate_k(k)
    half = k // 2
    g = nx.Graph()

    for j in range(1, half + 1):
        for i in range(1, half + 1):
            node = f"core-{j}-{i}"
            g.add_node(node, type="core", layer="core", ip=f"10.{k}.{j}.{i}", j=j, i=i)

    for pod in range(k):
        for s in range(half):
            g.add_node(f"edge-p{pod}-s{s}", type="edge", layer="edge",
                       pod=pod, switch=s, ip=f"10.{pod}.{s}.1")
        for s in range(half, k):
            g.add_node(f"agg-p{pod}-s{s}", type="agg", layer="agg",
                       pod=pod, switch=s, ip=f"10.{pod}.{s}.1")

        # edge <-> agg: complete bipartite within the pod
        for e in range(half):
            edge_node = f"edge-p{pod}-s{e}"
            for a in range(half, k):
                g.add_edge(edge_node, f"agg-p{pod}-s{a}", layer="edge-agg")

        # hosts: half hosts per edge switch, ID 2..half+1
        for e in range(half):
            edge_node = f"edge-p{pod}-s{e}"
            for host_id in range(2, half + 2):
                host_node = f"host-p{pod}-s{e}-h{host_id}"
                g.add_node(host_node, type="host", layer="host", pod=pod, switch=e,
                           host_id=host_id, ip=f"10.{pod}.{e}.{host_id}")
                g.add_edge(host_node, edge_node, layer="host-edge")

        # agg <-> core: agg switch a connects to core row j = local_index + 1
        for a in range(half, k):
            j = (a - half) + 1
            agg_node = f"agg-p{pod}-s{a}"
            for i in range(1, half + 1):
                g.add_edge(agg_node, f"core-{j}-{i}", layer="agg-core")

    return g


def validate(g, k):
    """Sanity-check a built fat-tree against the closed-form counts (paper Sec. 2)."""
    half = k // 2
    hosts = [n for n, d in g.nodes(data=True) if d["type"] == "host"]
    switches = [n for n, d in g.nodes(data=True) if d["type"] != "host"]

    expected_hosts = k**3 // 4
    expected_switches = 5 * k**2 // 4
    expected_links_per_layer = k**3 // 4

    if len(hosts) != expected_hosts:
        raise AssertionError(f"hosts {len(hosts)} != expected {expected_hosts}")
    if len(switches) != expected_switches:
        raise AssertionError(f"switches {len(switches)} != expected {expected_switches}")

    for layer in ("host-edge", "edge-agg", "agg-core"):
        count = sum(1 for _, _, d in g.edges(data=True) if d["layer"] == layer)
        if count != expected_links_per_layer:
            raise AssertionError(f"{layer} links {count} != expected {expected_links_per_layer}")

    core_count = sum(1 for _, d in g.nodes(data=True) if d["type"] == "core")
    if core_count != half * half:
        raise AssertionError(f"core switches {core_count} != expected {half * half}")

    return True


def build_traditional_tree(k):
    """Return a networkx.Graph for a conventional, oversubscribed 3-tier tree with
    the same host count as the k-ary fat-tree, for comparison.

    Matches the paper's Sec. 2 / Fig. 1 description of a common (pre-fat-tree) data
    center topology: a single aggregation switch per pod and a single core switch
    for the whole network, so there is exactly one path between any two hosts (no
    redundancy above the edge layer) -- unlike the fat-tree's Clos structure, which
    gives (k/2)^2 parallel core paths between any two pods.

    Same node/edge attribute shape as build_fat_tree() (type, layer, ip, pod, ...),
    so failures.py / metrics.py / visualize.py work on either topology unchanged.
    """
    validate_k(k)
    half = k // 2
    g = nx.Graph()

    g.add_node("core-1-1", type="core", layer="core", ip=f"10.{k}.1.1", j=1, i=1)

    for pod in range(k):
        agg_node = f"agg-p{pod}-s{half}"
        g.add_node(agg_node, type="agg", layer="agg", pod=pod, switch=half, ip=f"10.{pod}.{half}.1")
        g.add_edge(agg_node, "core-1-1", layer="agg-core")

        for e in range(half):
            edge_node = f"edge-p{pod}-s{e}"
            g.add_node(edge_node, type="edge", layer="edge",
                       pod=pod, switch=e, ip=f"10.{pod}.{e}.1")
            g.add_edge(edge_node, agg_node, layer="edge-agg")

            for host_id in range(2, half + 2):
                host_node = f"host-p{pod}-s{e}-h{host_id}"
                g.add_node(host_node, type="host", layer="host", pod=pod, switch=e,
                           host_id=host_id, ip=f"10.{pod}.{e}.{host_id}")
                g.add_edge(host_node, edge_node, layer="host-edge")

    return g


def validate_traditional_tree(g, k):
    """Sanity-check a built traditional tree: host count matches the fat-tree's,
    exactly one agg switch per pod, exactly one core switch, fully connected."""
    half = k // 2
    hosts = [n for n, d in g.nodes(data=True) if d["type"] == "host"]
    edge_switches = [n for n, d in g.nodes(data=True) if d["type"] == "edge"]
    agg_switches = [n for n, d in g.nodes(data=True) if d["type"] == "agg"]
    core_switches = [n for n, d in g.nodes(data=True) if d["type"] == "core"]

    expected_hosts = k**3 // 4
    if len(hosts) != expected_hosts:
        raise AssertionError(f"hosts {len(hosts)} != expected {expected_hosts}")
    if len(edge_switches) != k * half:
        raise AssertionError(f"edge switches {len(edge_switches)} != expected {k * half}")
    if len(agg_switches) != k:
        raise AssertionError(f"agg switches {len(agg_switches)} != expected {k}")
    if len(core_switches) != 1:
        raise AssertionError(f"core switches {len(core_switches)} != expected 1")
    if not nx.is_connected(g):
        raise AssertionError("traditional tree is not connected")

    return True


TOPOLOGY_TYPES = {"fat-tree": build_fat_tree, "traditional-tree": build_traditional_tree}


def build_topology(k, topology_type="fat-tree"):
    """Dispatch to build_fat_tree() or build_traditional_tree() by config/CLI name."""
    try:
        builder = TOPOLOGY_TYPES[topology_type]
    except KeyError:
        raise ValueError(f"unknown topology.type: {topology_type!r}, must be one of {set(TOPOLOGY_TYPES)}")
    return builder(k)


def validate_topology(g, k, topology_type="fat-tree"):
    """Dispatch to the matching validate() for topology_type."""
    if topology_type == "fat-tree":
        return validate(g, k)
    if topology_type == "traditional-tree":
        return validate_traditional_tree(g, k)
    raise ValueError(f"unknown topology.type: {topology_type!r}")
