from abc import abstractmethod
from typing import Any

import networkx as nx

from scythe import parsers


class ASTRemoval(parsers.TreeTraversal):
    def __init__(self, content: str, graph: nx.DiGraph) -> None:
        self.content = content
        self.graph = graph
        self.removals: list[tuple[int, int]] = []
        self.replacements: list[dict[str, Any]] = []

    @abstractmethod
    def remove_nodes(self, nodes_to_remove: set, mode: str) -> str:
        pass

    @classmethod
    def build_candidate(cls, base_content, graph, removed, mode, table=None):
        return cls._slow_candidate(base_content, graph, set(removed), mode), table

    @classmethod
    def _slow_candidate(cls, base_content, graph, sel, mode):
        inst = cls(base_content, graph)
        if mode == "break":
            return inst.break_inheritance(
                {n for n in sel if n.node_type == "class"})
        if mode == "flatten":
            return inst.flatten_inheritance(sel)
        return inst.remove_nodes(sel, mode)
