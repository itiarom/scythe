import os
import re
import hashlib
import threading
import concurrent.futures as cf
from dataclasses import dataclass

import picire

from scythe import utils
from scythe.graph import build_graph_from_file
from scythe.passes import C_FINE, LANGUAGES
from scythe.rewrites import AST_REMOVALS

PROBE_WORKERS = min(12, max(2, (os.cpu_count() or 4) - 2))
_TOKEN = re.compile(rb"\w+|[^\s\w]")


def token_estimate(content):
    return len(_TOKEN.findall(content.encode("utf-8")))


class Interesting:
    def __init__(self, graph, content, prop_checker, language, deferral=None):
        if prop_checker.check_initial() != 0:
            raise Exception(
                "The given property does not hold; the test script failed")
        cfg = LANGUAGES[language]
        self.graph = graph
        self.prop_checker = prop_checker
        self.content = content
        self.base_content = content
        self.language = language
        self.removal_mode = cfg.mode
        self.shrink_only = cfg.shrink_only
        self.deferral = deferral
        self.mode = None
        self.oracle_runs = 0
        self.prop_cache = {}
        self._lock = threading.Lock()
        self._base_tokens = (None, 0)
        self.reset_state()

    def reset_state(self):
        self.cache = {}
        self._table_src = None
        self._table = None

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

        content = self._build_candidate(nodes_to_remove, mode)
        res = (picire.Outcome.FAIL if self._oracle(content) == 0
               else picire.Outcome.PASS)
        with self._lock:
            self.cache[fr_nodes] = res
        return res

    def _build_candidate(self, nodes_to_remove, mode):
        if self._table_src is not self.base_content:
            self._table = None
            self._table_src = self.base_content
        content, self._table = AST_REMOVALS[self.language].build_candidate(
            self.base_content, self.graph, nodes_to_remove, mode, self._table)
        return content

    def _oracle(self, content):
        if self.shrink_only:
            if self._base_tokens[0] is not self.base_content:
                self._base_tokens = (self.base_content,
                                     token_estimate(self.base_content))
            if token_estimate(content) >= self._base_tokens[1]:
                return 1
        key = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self._lock:
            if key in self.prop_cache:
                return self.prop_cache[key]
            self.oracle_runs += 1
        out = self.prop_checker.run_oracle(content)
        with self._lock:
            self.prop_cache[key] = out
        return out

    def _materialize_winner(self, all_nodes, kept_nodes, mode):
        kept = set(kept_nodes)
        removed = {n for n in all_nodes if n not in kept}
        if not removed:
            return
        content = self._build_candidate(removed, mode)
        key = hashlib.sha256(content.encode("utf-8")).hexdigest()
        out = self.prop_cache.get(key)
        if out != 0:
            self.oracle_runs += 1
            out = self.prop_checker.run_oracle(content)
            self.prop_cache[key] = out
        if out == 0:
            self.content = content
            utils.update_file(self.prop_checker.file_path, content)


def perform_dd(interesting, node_filter):
    interesting.base_content = interesting.content
    nodes = [n for n in interesting.graph.nodes() if node_filter(n)]
    if len(nodes) <= 1:
        kept = list(nodes)
        if nodes and interesting.remove_definitions(
                set(), interesting.removal_mode) == picire.Outcome.FAIL:
            kept = []
    else:
        dd = picire.ParallelDD(
            interesting,
            cache=picire.parallel_dd.SharedCache(
                picire.cache.ConfigCache(cache_fail=True)),
            split=picire.splitter.BalancedSplit(n=2),
            dd_star=False,
            config_iterator=picire.iterator.CombinedIterator(
                False, picire.iterator.skip, picire.iterator.random))
        try:
            kept = list(dd(nodes))
        except picire.exception.ReductionError as e:
            print(f"Reduction error: {e}")
            interesting.reset_state()
            return
    interesting._materialize_winner(nodes, kept, interesting.removal_mode)
    interesting.reset_state()


def _node_range(n):
    a = n.args
    if isinstance(a, tuple) and len(a) >= 2 \
            and isinstance(a[0], int) and isinstance(a[1], int):
        return (a[0], a[1])
    return None


def _range_tree(nodes):
    ranges = {n: _node_range(n) for n in nodes}

    def contains(a, b):
        ra, rb = ranges[a], ranges[b]
        return (ra is not None and rb is not None and a is not b
                and ra[0] <= rb[0] and rb[1] <= ra[1] and ra != rb)

    def children(node):
        inside = [c for c in nodes if contains(node, c)]
        return [c for c in inside
                if not any(contains(o, c) for o in inside if o is not c)]

    roots = [c for c in nodes if not any(contains(o, c) for o in nodes)]
    return roots, children, lambda n: frozenset([n])


@dataclass(frozen=True)
class _Pseudo:
    tag: str
    payload: tuple

    def __str__(self):
        return f"{self.tag}{self.payload}"


def _dominator_tree(graph, nodes):
    nodeset = set(nodes)
    kids = {n: [v for v in graph.successors(n)
                if v in nodeset and graph[n][v].get("dominates")]
            for n in nodes}
    dominated = {v for vs in kids.values() for v in vs}
    if not dominated:
        return None
    below = {}

    def members(n):
        if isinstance(n, _Pseudo):
            return frozenset(n.payload)
        if n not in below:
            out, stack = {n}, list(kids[n])
            while stack:
                v = stack.pop()
                out.add(v)
                stack.extend(kids[v])
            below[n] = frozenset(out)
        return below[n]

    def children(n):
        if isinstance(n, _Pseudo) or not kids[n]:
            return []
        return kids[n] + [_Pseudo("alone", (n,))]

    return [n for n in nodes if n not in dominated], children, members


def _group_tree(nodes):
    groups = {}
    for n in nodes:
        groups.setdefault((n.node_type, (n.name or "").split(":", 1)[0]), []).append(n)
    if all(len(g) == 1 for g in groups.values()):
        return None
    roots = [_Pseudo("group", tuple(g)) if len(g) > 1 else g[0]
             for g in groups.values()]

    def members(n):
        return frozenset(n.payload) if isinstance(n, _Pseudo) else frozenset([n])

    def children(n):
        return list(n.payload) if isinstance(n, _Pseudo) else []

    return roots, children, members


def _probe_tree(graph, nodes):
    funcs = [n for n in nodes if n.node_type == "function"]
    grouped = [n for n in nodes if n.node_type in C_FINE]
    rest = [n for n in nodes
            if n.node_type != "function" and n.node_type not in C_FINE]
    parts = []
    for tree, members in ((_dominator_tree(graph, funcs) if funcs else None, funcs),
                          (_group_tree(grouped) if grouped else None, grouped)):
        if tree is None:
            rest.extend(members)
        else:
            parts.append(tree)
    if rest:
        parts.append(_range_tree(rest))
    if len(parts) == 1:
        return parts[0]
    owner, roots = {}, []
    for t in parts:
        for r in t[0]:
            owner[r] = t
            roots.append(r)

    def children(n):
        kids = owner[n][1](n)
        for k in kids:
            owner[k] = owner[n]
        return kids

    return roots, children, lambda n: owner[n][2](n)


def _probe_key(n):
    if isinstance(n, _Pseudo):
        return ("", str(n))
    return (str(_node_range(n) or ""), n.name or "")


def _coarse_probe(interesting, nodes, mode, deferral=None, tree=None):
    roots, children, members = tree or _range_tree(nodes)

    def holds(n):
        return interesting._oracle(
            interesting._build_candidate(set(members(n)), mode)) == 0

    def probe_ok(n):
        return isinstance(n, _Pseudo) or deferral is None \
            or deferral.should_probe(n)

    frontier = sorted(roots, key=_probe_key)
    removable, covered = [], set()
    while frontier:
        to_probe = [n for n in frontier if probe_ok(n)]
        with cf.ThreadPoolExecutor(max_workers=PROBE_WORKERS) as ex:
            verdicts = dict(zip(to_probe, ex.map(holds, to_probe)))
        nxt = []
        for n in frontier:
            if verdicts.get(n, False):
                group = members(n) - covered
                if group:
                    removable.append(group)
                    covered |= group
            else:
                if deferral is not None and n in verdicts \
                        and not isinstance(n, _Pseudo):
                    deferral.record_failure(n)
                nxt.extend(c for c in children(n)
                           if not (members(c) <= covered))
        frontier = sorted(set(nxt), key=_probe_key)
    return removable


def _commit_max(interesting, removable, mode):
    groups = [g if isinstance(g, frozenset) else frozenset([g])
              for g in removable]

    def build(subset):
        return interesting._build_candidate(
            set().union(*subset) if subset else set(), mode)

    def holds(subset):
        return interesting._oracle(build(subset)) == 0

    def find(base, cands):
        if holds(base + cands):
            return cands
        if len(cands) == 1:
            return []
        mid = len(cands) // 2
        left = find(base, cands[:mid])
        right = find(base + left, cands[mid:])
        return left + right

    to_remove = find([], groups)
    return build(to_remove) if to_remove else None


def parallel_probe_reduce(interesting, node_filter, union_first=False):
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
        committed = None
        if union_first:
            candidate = interesting._build_candidate(set(nodes), mode)
            if interesting._oracle(candidate) == 0:
                committed = candidate
        if committed is None:
            # Builds the shared candidate table before the parallel probes.
            interesting._build_candidate({nodes[0]}, mode)
            if interesting.deferral is not None:
                interesting.deferral.begin_round(interesting.base_content, nodes)
            removable = _coarse_probe(interesting, nodes, mode,
                                      interesting.deferral,
                                      _probe_tree(interesting.graph, nodes))
            if not removable:
                break
            committed = _commit_max(interesting, removable, mode)
        if committed is None or committed == interesting.base_content:
            break
        interesting.content = committed
        utils.update_file(fp, committed)
    interesting.reset_state()
