import os
import hashlib
import threading
import traceback
import concurrent.futures as cf

import networkx as nx
import picire

from scythe import utils
from scythe.graph import build_graph_from_file
from scythe.rewrites import AST_REMOVALS

PROBE_WORKERS = min(8, max(2, (os.cpu_count() or 4) - 2))


class Interesting():
    def __init__(self, graph: nx.DiGraph,
                 content,
                 prop_checker,
                 language: str,
                 mode: str):
        self.graph = graph
        self.prop_checker = prop_checker
        self.content = content
        self.base_content = content
        self.language = language

        if prop_checker.check_initial() != 0:
            raise Exception(
                "The given property does not hold; the test script failed")

        self.reset_state()
        self.mode = None
        self.removal_mode = mode
        self._lock = threading.Lock()
        self.prop_cache = {}

    def reset_state(self):
        self.cache = {}
        self._engine_src = None
        self._engine_table = None

    def __call__(self, nodes, config_id):
        return self.remove_definitions(nodes, self.removal_mode)

    def remove_definitions(self, nodes, mode):
        nodes_to_remove = [
            n for n in self.graph.nodes()
            if n.node_type in self.mode and n not in nodes
        ]
        fr_nodes = frozenset(nodes)
        if fr_nodes in self.cache:
            return self.cache.get(fr_nodes)
        if not nodes_to_remove:
            return picire.Outcome.FAIL

        # picire test callback: build the candidate from the frozen base_content
        # and probe the oracle. The winner is committed in _materialize_winner.
        content = self._build_candidate(nodes_to_remove, mode)
        res = (picire.Outcome.FAIL if self._oracle(content) == 0
               else picire.Outcome.PASS)
        with self._lock:
            self.cache[fr_nodes] = res
        return res

    def _build_candidate(self, nodes_to_remove, mode):
        if self._engine_src is not self.base_content:
            self._engine_table = None
            self._engine_src = self.base_content
        content, self._engine_table = AST_REMOVALS[self.language].build_candidate(
            self.base_content, self.graph, nodes_to_remove, mode,
            self._engine_table)
        return content

    def _oracle(self, content):
        key = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self._lock:
            if key in self.prop_cache:
                return self.prop_cache[key]
        out = self.prop_checker.run_oracle(content)
        with self._lock:
            self.prop_cache[key] = out
        return out

    def _materialize_winner(self, all_nodes, kept_nodes, mode):
        kept = set(kept_nodes)
        removed = {n for n in all_nodes if n not in kept}
        content = self._build_candidate(removed, mode)
        key = hashlib.sha256(content.encode("utf-8")).hexdigest()
        out = self.prop_cache.get(key)
        if out != 0:
            out = self.prop_checker.run_oracle(content)
            self.prop_cache[key] = out
        if out == 0:
            self.content = content
            utils.update_file(self.prop_checker.file_path, content)

    def update_inheritance_tree(self, node):
        parents = list(self.graph.predecessors(node))
        children = set()
        for child, data in self.graph[node].items():
            if data["label"] == "inherits":
                children.add(child)
        for child in children:
            self.graph.remove_edge(node, child)
            for parent in parents:
                self.graph.add_edge(parent, child, label="inherits")

    def update_graph(self, nodes_to_remove, remove_contracts=False):
        nodes = set()
        excluded_nodes = set()
        for node in nodes_to_remove:
            if remove_contracts:
                self.update_inheritance_tree(node)
            if node not in self.graph:
                continue
            for k, v, label in nx.dfs_labeled_edges(self.graph, source=node):
                if label == "inherits":
                    excluded_nodes.add(v)
                    continue

                if k in excluded_nodes:
                    excluded_nodes.add(v)
                    continue

                nodes.add(k)
                nodes.add(v)
        self.graph.remove_nodes_from(nodes)


def perform_dd(interesting, node_filter, parallel: bool = False):
    dd_cls = picire.ParallelDD if parallel else picire.DD
    interesting.base_content = interesting.content
    nodes = [n for n in interesting.graph.nodes() if node_filter(n)]
    if len(nodes) <= 1:
        output_nodes = list(nodes)
        if nodes and interesting.remove_definitions(
                set(), interesting.removal_mode) == picire.Outcome.FAIL:
            output_nodes = []
        interesting._materialize_winner(
            nodes, output_nodes, interesting.removal_mode)
        interesting.update_graph(
            [f for f in nodes if f not in output_nodes], remove_contracts=True)
        interesting.reset_state()
        return
    cache = picire.parallel_dd.SharedCache(
        picire.cache.ConfigCache(cache_fail=True))
    dd_star = False
    dd_obj = dd_cls(
        interesting,
        cache=cache,
        split=picire.splitter.BalancedSplit(n=2),
        dd_star=dd_star,
        config_iterator=picire.iterator.CombinedIterator(
            False, picire.iterator.skip, picire.iterator.random
        )
    )
    try:
        output_nodes = [x for x in dd_obj(nodes)]
    except picire.exception.ReductionError as e:
        interesting.reset_state()
        print("Reduction error")
        print(traceback.format_exc())
        return
    interesting._materialize_winner(
        nodes, output_nodes, interesting.removal_mode)
    interesting.update_graph(
        [f for f in nodes if f not in output_nodes],
        remove_contracts=True,
    )
    interesting.reset_state()


def _node_range(n):
    a = n.args
    if isinstance(a, tuple) and len(a) >= 2 \
            and isinstance(a[0], int) and isinstance(a[1], int):
        return (a[0], a[1])
    return None


def _coarse_probe(interesting, nodes, mode):
    # Coarse-first probe for containment-structured candidates (blocks /
    # expressions): try the HUGE outermost ones first. An accepted collapse
    # subsumes everything nested in it, so its children are never probed; a
    # rejected one is descended into (probe its direct children) — which both
    # prunes wasted probes and provides the inner-expression fallback. For flat
    # candidates (functions/globals have no byte range) every node is outermost,
    # so this is one parallel level == the old flat probe.
    ranges = {n: _node_range(n) for n in nodes}

    def contains(a, b):
        ra, rb = ranges[a], ranges[b]
        return (ra is not None and rb is not None and a is not b
                and ra[0] <= rb[0] and rb[1] <= ra[1] and ra != rb)

    def children(node):
        inside = [c for c in nodes if contains(node, c)]
        return [c for c in inside
                if not any(contains(o, c) for o in inside if o is not c)]

    def key(n):
        return (ranges[n] or (0, 0), n.name or "")

    def holds(n):
        return interesting._oracle(
            interesting._build_candidate({n}, mode)) == 0

    frontier = sorted((c for c in nodes
                       if not any(contains(o, c) for o in nodes)), key=key)
    removable = []
    while frontier:
        with cf.ThreadPoolExecutor(max_workers=PROBE_WORKERS) as ex:
            verdicts = list(ex.map(holds, frontier))
        nxt = set()
        for n, ok in zip(frontier, verdicts):
            if ok:
                removable.append(n)
            else:
                nxt.update(children(n))
        frontier = sorted(nxt, key=key)
    return removable


def _commit_max(interesting, removable, mode):
    # Largest jointly-removable subset; whole union is 1 oracle call when it
    # holds, else a divide-and-conquer search keeps the invariant that removing
    # base+R is oracle-valid, so every committed program is validated.
    def holds(subset):
        return interesting._oracle(
            interesting._build_candidate(set(subset), mode)) == 0

    def find(base, cands):
        if holds(base + cands):
            return cands
        if len(cands) == 1:
            return []
        mid = len(cands) // 2
        left = find(base, cands[:mid])
        right = find(base + left, cands[mid:])
        return left + right

    to_remove = find([], list(removable))
    return interesting._build_candidate(set(to_remove), mode) if to_remove \
        else None


def parallel_probe_reduce(interesting, node_filter):
    # Fixpoint replacement for ddmin: each round coarse-first-probes candidates
    # from a frozen snapshot (huge/outermost first; accepted collapses subsume
    # their nested candidates, rejected ones are descended into), commits the
    # largest jointly-removable subset, and repeats so cascades are caught.
    # O(n) probes/round (vs ddmin's ~O(n^2)) and parallel within each level.
    mode = interesting.removal_mode
    fp = interesting.prop_checker.file_path
    while True:
        interesting.graph = build_graph_from_file(fp, interesting.language)
        interesting.base_content = interesting.content
        interesting.reset_state()
        nodes = sorted((n for n in interesting.graph.nodes() if node_filter(n)),
                       key=lambda n: n.name or "")
        if not nodes:
            break
        interesting._build_candidate({nodes[0]}, mode)  # warm the edit table
        removable = _coarse_probe(interesting, nodes, mode)
        if not removable:
            break
        committed = _commit_max(interesting, removable, mode)
        if committed is None or committed == interesting.base_content:
            break
        interesting.content = committed
        utils.update_file(fp, committed)
    interesting.reset_state()
