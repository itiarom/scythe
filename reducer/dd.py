import os
import shutil
import string
import random
import hashlib
import tempfile
import threading
import traceback

import networkx as nx
import picire

from reducer import utils
from reducer.modifications import AST_REMOVALS


class Interesting():
    EXT = {"solidity": "sol", "c": "c", "java": "java"}

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

        if language == "java":
            _wd = tempfile.mkdtemp(prefix="greduce_")
            try:
                res = prop_checker.run_test_script(None, cwd=_wd)
            finally:
                shutil.rmtree(_wd, ignore_errors=True)
        else:
            res = prop_checker.run_test_script(None)
        if res is None or res != 0:
            raise Exception(
                "The given property does not hold; the test script failed")

        self.reset_state()
        self.mode = None
        self.removal_mode = mode
        self._lock = threading.Lock()
        self.prop_cache = {}

    def reset_state(self):
        self.cache = {}
        self.removed_nodes = set()
        self._repl_src = None
        self._repl_table = None

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

        if self.language == "java":
            content = self._build_candidate(nodes_to_remove, mode)
            res = (picire.Outcome.FAIL if self._oracle(content) == 0
                   else picire.Outcome.PASS)
            with self._lock:
                self.cache[fr_nodes] = res
            return res

        new_content = self.test_removing_definitions(nodes_to_remove, mode)
        if new_content is not None:
            self.content = new_content
            utils.update_file(self.prop_checker.file_path, new_content)
            res = picire.Outcome.FAIL
        else:
            res = picire.Outcome.PASS
        self.cache[fr_nodes] = res
        return res

    def _build_candidate(self, nodes_to_remove, mode):
        sel = set(nodes_to_remove)
        if mode == "replacement" and sel \
                and all(n.node_type in ("function", "field", "local_variable")
                        for n in sel):
            cls = AST_REMOVALS[self.language]
            if self._repl_src is not self.base_content:
                self._repl_table = cls(self.base_content,
                                       self.graph).replacement_table()
                self._repl_src = self.base_content
            return cls.assemble_from_table(
                self.base_content, sel, self._repl_table)
        ast_removal = AST_REMOVALS[self.language](self.base_content, self.graph)
        if mode == "break":
            return ast_removal.break_inheritance(
                {n for n in sel if n.node_type == "class"})
        if mode == "flatten":
            return ast_removal.flatten_inheritance(sel)
        return ast_removal.remove_nodes(sel, mode)

    def _oracle(self, content):
        key = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self._lock:
            if key in self.prop_cache:
                return self.prop_cache[key]
        out = self._run_oracle(content)
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
            out = self._run_oracle(content)
            self.prop_cache[key] = out
        if out == 0:
            self.content = content
            utils.update_file(self.prop_checker.file_path, content)

    def test_removing_definitions(self, nodes_to_remove, mode):
        nodes_to_remove = set(nodes_to_remove).union(self.removed_nodes)
        ast_removal = AST_REMOVALS[self.language](self.content, self.graph)
        if mode == "break":
            nodes_to_remove = set(filter(lambda n: n.node_type == "class", nodes_to_remove))
            modified_content = ast_removal.break_inheritance(nodes_to_remove)
        elif mode == "flatten":
            modified_content = ast_removal.flatten_inheritance(nodes_to_remove)
        else:
            modified_content = ast_removal.remove_nodes(nodes_to_remove, mode)
        content_key = hashlib.sha256(modified_content.encode("utf-8")).hexdigest()
        if content_key in self.prop_cache:
            output = self.prop_cache[content_key]
        else:
            output = self._run_oracle(modified_content)
            self.prop_cache[content_key] = output

        if output == 0:
            self.removed_nodes = nodes_to_remove
            return modified_content
        return None

    def _run_oracle(self, content):
        name = ''.join(random.sample(string.ascii_letters + string.digits, 5))
        ext = self.EXT[self.language]
        if self.language == "java":
            workdir = tempfile.mkdtemp(prefix="greduce_")
            temp_file_path = os.path.join(workdir, f"{name}.{ext}")
            try:
                with open(temp_file_path, 'w') as temp_file:
                    temp_file.write(content)
                return self.prop_checker.run_test_script(temp_file_path,
                                                         cwd=workdir)
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
        temp_file_path = f"{name}.{ext}"
        with open(temp_file_path, 'w') as temp_file:
            temp_file.write(content)
        try:
            return self.prop_checker.run_test_script(temp_file_path)
        finally:
            os.remove(temp_file_path)

    def get_contract_by_name(self, contract_name):
        nodes = [n for n in self.graph.nodes()
                 if n.node_type == "contract" and n.name == contract_name]
        assert len(nodes) == 1
        return nodes[0]

    def update_parse_tree(self):
        self.tree = AST_REMOVALS[self.language].setup_parse_tree(self.content)
        self.content_ = self.content

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


def perform_dd(
    interesting, node_filter, parallel: bool = False, language: str = 'solidity'
):
    dd_cls = picire.ParallelDD if parallel else picire.DD
    interesting.base_content = interesting.content
    nodes = [n for n in interesting.graph.nodes() if node_filter(n)]
    if len(nodes) <= 1:
        output_nodes = list(nodes)
        if nodes and interesting.remove_definitions(
                set(), interesting.removal_mode) == picire.Outcome.FAIL:
            output_nodes = []
        if interesting.language == "java":
            interesting._materialize_winner(
                nodes, output_nodes, interesting.removal_mode)
        interesting.update_graph(
            [f for f in nodes if f not in output_nodes], remove_contracts=True)
        interesting.reset_state()
        return
    cache = picire.parallel_dd.SharedCache(
        picire.cache.ConfigCache(cache_fail=True))
    dd_star = language == "c"
    dd_obj = dd_cls(
        interesting,
        cache=cache,
        split=picire.splitter.BalancedSplit(n=2),
        dd_star=dd_star,
        config_iterator=picire.iterator.CombinedIterator(
            False, picire.iterator.skip,
            picire.iterator.random if language != 'c' else picire.iterator.backward
        )
    )
    try:
        output_nodes = [x for x in dd_obj(nodes)]
    except picire.exception.ReductionError as e:
        interesting.reset_state()
        print("Reduction error")
        print(traceback.format_exc())
        return
    if interesting.language == "java":
        interesting._materialize_winner(
            nodes, output_nodes, interesting.removal_mode)
    interesting.update_graph(
        [f for f in nodes if f not in output_nodes],
        remove_contracts=True,
    )
    interesting.reset_state()
