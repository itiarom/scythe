from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, NamedTuple, Optional

import networkx as nx

from scythe import parsers, utils


class DeclarationNode(NamedTuple):
    name: str
    node_type: str
    parent: Any
    args: Optional[List[str]] = None

    def __hash__(self):
        return hash((self.name, self.node_type, self.parent, self.args))

    def __str__(self):
        node_name = f"{self.node_type}[{self.name}]"
        if self.parent is not None:
            return f"{str(self.parent)}.{node_name}"
        return node_name

    def __repr__(self) -> str:
        return self.__str__()


@dataclass(frozen=True)
class InheritSpec:
    parents: Callable
    target: str
    lenient: bool = False


@dataclass(frozen=True)
class NodeSpec:
    node_type: str
    names: Optional[Callable] = None
    sig: Optional[Callable] = None
    scope: bool = False
    parent_none: bool = False
    edge_label: str = "def"
    register: bool = False
    inherit: Optional[InheritSpec] = None
    type_use: Optional[Callable] = None


@dataclass
class LanguageSchema:
    language: str
    specs: Dict[str, NodeSpec] = field(default_factory=dict)
    type_nodes: tuple = ()


class GraphBuilder(parsers.TreeTraversal):
    SCHEMA: Optional[LanguageSchema] = None

    def __init__(self) -> None:
        self.graph = nx.DiGraph()
        self.declaration_stack: List[DeclarationNode] = []
        self.registry: Dict[str, Dict[str, DeclarationNode]] = {}
        self._scope_nodes: List[int] = []
        self._pending_type_uses: list = []

    def peek_declaration(self) -> Optional[DeclarationNode]:
        return self.declaration_stack[-1] if self.declaration_stack else None

    def push_declaration(self, node: DeclarationNode) -> None:
        self.declaration_stack.append(node)

    def pop_declaration(self) -> Optional[DeclarationNode]:
        return self.declaration_stack.pop() if self.declaration_stack else None

    def build_graph(self, source_file: str) -> nx.DiGraph:
        assert self.SCHEMA is not None, "SCHEMA must be set in subclasses"
        tree = parsers.parse(source_file, self.SCHEMA.language)
        self.traverse_node(tree.root_node)
        self._finalize_type_uses()
        return self.graph

    def get_node_visitor(self, node):
        return self._visit

    def get_node_exit(self, node):
        return self._exit

    def _visit(self, node) -> None:
        spec = self.SCHEMA.specs.get(node.type)
        if spec is not None:
            self._emit_spec(node, spec)

    def _exit(self, node) -> None:
        if self._scope_nodes and self._scope_nodes[-1] == node.id:
            self._scope_nodes.pop()
            self.pop_declaration()

    def _emit_spec(self, node, spec: NodeSpec) -> None:
        names_fn = spec.names or (lambda n: [parsers.declaration_name(n)])
        parent = None if spec.parent_none else self.peek_declaration()
        sig = spec.sig(node) if spec.sig else None
        first = None
        for name in names_fn(node):
            decl = DeclarationNode(name, spec.node_type, parent, sig)
            self.graph.add_node(decl)
            if parent is not None and not spec.parent_none:
                self.graph.add_edge(parent, decl, label=spec.edge_label)
            if spec.register:
                self.registry.setdefault(spec.node_type, {})[name] = decl
            if spec.type_use is not None:
                self._pending_type_uses.append((decl, set(spec.type_use(node))))
            if first is None:
                first = decl
        if spec.scope and first is not None:
            self.push_declaration(first)
            self._scope_nodes.append(node.id)
            if spec.inherit is not None:
                self._process_inherit(first, node, spec.inherit)

    def _process_inherit(self, decl, node, inherit: InheritSpec) -> None:
        index = self.registry.get(inherit.target, {})
        for parent_name in inherit.parents(node):
            if parent_name == decl.name:
                continue
            parent = index.get(parent_name) if inherit.lenient else index[parent_name]
            if parent is None:
                continue
            self.graph.add_edge(parent, decl, label="inherits")

    def _finalize_type_uses(self) -> None:
        if not self._pending_type_uses:
            return
        type_nodes = {n.name: n for n in self.graph.nodes
                      if n.node_type in self.SCHEMA.type_nodes}
        for decl, type_names in self._pending_type_uses:
            for type_name in type_names:
                target = type_nodes.get(type_name)
                if target is not None and target is not decl:
                    self.graph.add_edge(target, decl, label="uses-type")


def _sol_inherit_parents(node):
    return [c.text.decode("utf-8") for c in node.children
            if c.type == "inheritance_specifier"]


_SOL_CONTRACT = NodeSpec(
    "contract", scope=True, parent_none=True, register=True,
    inherit=InheritSpec(_sol_inherit_parents, "contract", lenient=False))

SOLIDITY = LanguageSchema(
    language="solidity",
    type_nodes=("contract", "struct"),
    specs={
        "contract_declaration": _SOL_CONTRACT,
        "interface_declaration": _SOL_CONTRACT,
        "function_definition": NodeSpec(
            "function", sig=parsers.parameter_signature, scope=True),
        "modifier_definition": NodeSpec("modifier"),
        "event_definition": NodeSpec("event"),
        "struct_declaration": NodeSpec("struct", scope=True),
        "state_variable_declaration": NodeSpec(
            "state_var", type_use=parsers.type_reference_names),
        "variable_declaration": NodeSpec(
            "var", type_use=parsers.type_reference_names),
    },
)


def _first_identifier(node):
    for child in node.children:
        if child.type == "identifier":
            return [child.text.decode("utf-8")]
    return []


def _java_method_sig(node):
    for child in node.children:
        if child.type == "formal_parameters":
            return child.text.decode("utf-8")
    return None


def _java_declarator_names(node):
    names = []
    for child in node.children:
        if child.type == "variable_declarator":
            name_node = child.child_by_field_name("name")
            if name_node:
                names.append(name_node.text.decode("utf-8"))
    return names


def _java_inherit_parents(node):
    return [c.text.decode("utf-8").split("extends ")[-1]
            for c in node.children if c.type == "superclass"]


_JAVA_CLASS = NodeSpec(
    "class", names=_first_identifier, scope=True, parent_none=True, register=True,
    inherit=InheritSpec(_java_inherit_parents, "class", lenient=True))

JAVA = LanguageSchema(
    language="java",
    specs={
        "class_declaration": _JAVA_CLASS,
        "interface_declaration": _JAVA_CLASS,
        "method_declaration": NodeSpec(
            "function", names=_first_identifier, sig=_java_method_sig, scope=True),
        "constructor_declaration": NodeSpec(
            "constructor", names=_first_identifier, sig=_java_method_sig, scope=True),
        "field_declaration": NodeSpec("field", names=_java_declarator_names),
        "local_variable_declaration": NodeSpec(
            "local_variable", names=_java_declarator_names),
    },
)


class SolidityGraphBuilder(GraphBuilder):
    SCHEMA = SOLIDITY


class JavaGraphBuilder(GraphBuilder):
    SCHEMA = JAVA


class CGraphBuilder:

    def build_graph(self, source_file: str) -> nx.DiGraph:
        from scythe.cdeps import CModel
        return CModel(utils.read_file(source_file)).graph()


GRAPH_BUILDERS = {
    "solidity": SolidityGraphBuilder,
    "c": CGraphBuilder,
    "java": JavaGraphBuilder,
}


def get_graph_builder(language: str):
    builder = GRAPH_BUILDERS.get(language)
    if builder is None:
        raise Exception(
            f"Graph builder for language '{language}' was not found")
    return builder


def build_graph_from_file(file_path: str, language: str) -> nx.DiGraph:
    builder = get_graph_builder(language)
    return builder().build_graph(file_path)
