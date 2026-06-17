import os
import string
import random
import hashlib
import traceback

import networkx as nx
import picire  # type: ignore

from reducer import utils
from reducer.modifications import AST_REMOVALS


class Interesting():
    def __init__(self, graph: nx.DiGraph,
                 content,
                 prop_checker,
                 language: str,
                 mode: str):
        self.graph = graph
        self.prop_checker = prop_checker
        self.content = content
        self.language = language

        res = prop_checker.run_test_script(None)
        if res is None or res != 0:
            raise Exception(
                "The given property does not hold; the test script failed")

        self.reset_state()
        self.mode = None
        self.removal_mode = mode
        # Property-check results keyed by candidate *content* hash. Unlike
        # `self.cache` (reset every pass) this persists across all passes and
        # fixed-point rounds, so an identical candidate program is never sent to
        # the (≈1s) test script twice -- the dominant cost in reduction.
        self.prop_cache = {}

    def reset_state(self):
        self.cache = {}
        self.removed_nodes = set()

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
        new_content = self.test_removing_definitions(nodes_to_remove, mode)
        if new_content is not None:
            self.content = new_content
            utils.update_file(self.prop_checker.file_path, new_content)
            res = picire.Outcome.FAIL
        else:
            res = picire.Outcome.PASS
        self.cache[fr_nodes] = res
        return res

    def test_removing_definitions(self, nodes_to_remove, mode):
        nodes_to_remove = set(nodes_to_remove).union(self.removed_nodes)
        ast_removal = AST_REMOVALS[self.language](self.content,
                                                  self.graph)
        if mode == "break":
            nodes_to_remove = set(filter(lambda n: n.node_type == "class", nodes_to_remove))
            modified_content = ast_removal.break_inheritance(nodes_to_remove)
        elif mode == "flatten":
            modified_content = ast_removal.flatten_inheritance(nodes_to_remove)
        else:
            modified_content = ast_removal.remove_nodes(nodes_to_remove, mode)
        # Skip the expensive test script if this exact program was checked before.
        content_key = hashlib.sha256(modified_content.encode("utf-8")).hexdigest()
        if content_key in self.prop_cache:
            output = self.prop_cache[content_key]
        else:
            name = ''.join(random.sample(string.ascii_letters + string.digits, 5))
            if (self.language == 'solidity'):
                temp_file_path = f"{name}.sol"
            elif (self.language == 'c'):
                temp_file_path = f"{name}.c"
            elif (self.language == 'java'):
                temp_file_path = f"{name}.java"
            with open(temp_file_path, 'w') as temp_file:
                temp_file.write(modified_content)
            output = self.prop_checker.run_test_script(temp_file_path)
            os.remove(temp_file_path)
            self.prop_cache[content_key] = output

        if output == 0:
            # property is satisfied because the script returned exit code 0
            self.removed_nodes = nodes_to_remove
            return modified_content
        return None

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
    nodes = [n for n in interesting.graph.nodes() if node_filter(n)]
    if len(nodes) <= 1:
        # ddmin needs >= 2 elements to subdivide, so a single-candidate pass
        # would be returned untouched. Test removing the lone candidate directly.
        output_nodes = list(nodes)
        if nodes and interesting.remove_definitions(
                set(), interesting.removal_mode) == picire.Outcome.FAIL:
            output_nodes = []
        interesting.update_graph(
            [f for f in nodes if f not in output_nodes], remove_contracts=True)
        interesting.reset_state()
        return
    cache = picire.parallel_dd.SharedCache(
        picire.cache.ConfigCache(cache_fail=True))
    # Solidity and Java both re-run their passes to a fixed point (main.py), so
    # picire's own dd* intra-pass fixpoint is redundant there -- a single ddmin
    # sweep per pass plus the outer loop is already 1-minimal and roughly halves
    # the test-script calls. C runs the passes once, so it keeps dd*.
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
    interesting.update_graph(
        [f for f in nodes if f not in output_nodes],
        remove_contracts=True,
    )
    #interesting.update_parse_tree()
    interesting.reset_state()
