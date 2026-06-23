from abc import abstractmethod
from typing import Any

import networkx as nx

from scythe import parsers


def remove_empty_lines(source_code):
    lines = source_code.split("\n")
    non_empty_lines = [line for line in lines if line.strip() != ""]
    return "\n".join(non_empty_lines)


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
        """Candidate source for removing ``removed`` (the per-language rewrite
        dispatch, moved out of dd.py). ``table`` memoizes the replacement table
        across calls with the same ``base_content``; returns ``(content, table)``.
        Slow paths build a fresh instance per call (those methods mutate
        ``self.content``); fast paths are stateless classmethods."""
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
