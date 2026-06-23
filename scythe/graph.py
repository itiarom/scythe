from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, NamedTuple, Optional

import networkx as nx

from scythe import parsers

EXPRESSION_MIN_BYTES = 40


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


# --------------------------------------------------------------------------- #
# Language-agnostic schema: a builder is described by a table of node specs    #
# plus, for constructs that don't fit the declaration model, custom callables. #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class InheritSpec:
    parents: Callable          # ast node -> list[str] of parent names
    target: str                # registry node_type to resolve parents against
    lenient: bool = False      # skip unknown parents (Java) vs raise (Solidity)


@dataclass(frozen=True)
class NodeSpec:
    node_type: str
    names: Optional[Callable] = None   # ast node -> list[str]; default = declaration_name
    sig: Optional[Callable] = None
    scope: bool = False                # push as enclosing scope
    parent_none: bool = False          # force parent=None, emit no containment edge
    edge_label: str = "def"
    register: bool = False             # index by name for inheritance lookup
    inherit: Optional[InheritSpec] = None
    type_use: Optional[Callable] = None  # ast node -> iterable[str] (uses-type targets)


@dataclass
class LanguageSchema:
    language: str
    specs: Dict[str, NodeSpec] = field(default_factory=dict)
    custom: Dict[str, Callable] = field(default_factory=dict)
    custom_exit: Dict[str, Callable] = field(default_factory=dict)
    type_nodes: tuple = ()             # node_types resolvable as uses-type targets


class GraphBuilder(parsers.TreeTraversal):
    SCHEMA: Optional[LanguageSchema] = None

    def __init__(self) -> None:
        self.graph = nx.DiGraph()
        self.declaration_stack: List[DeclarationNode] = []
        self.registry: Dict[str, Dict[str, DeclarationNode]] = {}
        self.current_function: Optional[DeclarationNode] = None
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
        custom = self.SCHEMA.custom.get(node.type)
        if custom is not None:
            custom(self, node)
            return
        spec = self.SCHEMA.specs.get(node.type)
        if spec is not None:
            self._emit_spec(node, spec)

    def _exit(self, node) -> None:
        custom = self.SCHEMA.custom_exit.get(node.type)
        if custom is not None:
            custom(self, node)
            return
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


# --------------------------------------------------------------------------- #
# Solidity                                                                     #
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# Java                                                                         #
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# C — the declaration model plus value/block simplification fragments. These   #
# don't fit NodeSpec (anonymous, byte-range identity, current-function state), #
# so they stay as explicit handlers dispatched from the shared engine.         #
# --------------------------------------------------------------------------- #

_C_EXPR_VALUE_TYPES = ("binary_expression", "call_expression",
                       "conditional_expression", "parenthesized_expression")


def _c_add_function(builder, ident):
    name = ident.text.decode("utf-8")
    parent = builder.peek_declaration()
    func = DeclarationNode(name, "function", parent)
    builder.graph.add_node(func)
    builder.push_declaration(func)
    builder.symbols[name] = func
    builder.current_function = func
    if parent is not None:
        builder.graph.add_edge(parent, func, label="def")


def _c_function_definition(builder, node):
    for child in node.children:
        if child.type != "function_declarator":
            continue
        for cc in child.children:
            if cc.type == "identifier":
                _c_add_function(builder, cc)
                break
            if cc.type == "parenthesized_declarator":
                for ccc in cc.children:
                    if ccc.type == "identifier":
                        _c_add_function(builder, ccc)
                        break


def _c_exit_function_definition(builder, node):
    builder.pop_declaration()
    builder.current_function = None


def _c_add_global(builder, name):
    parent = builder.current_function or builder.peek_declaration()
    var = DeclarationNode(name, "global_variable", parent)
    builder.graph.add_node(var)
    builder.symbols[name] = var
    builder.push_declaration(var)
    if parent is not None:
        builder.graph.add_edge(parent, var, label="var")


def _c_declaration(builder, node):
    if not any(c.type == "storage_class_specifier"
               and c.text.decode("utf-8") == "static" for c in node.children):
        return
    # A static function prototype's name collides with the function node.
    if any(c.type == "function_declarator" for c in node.children):
        return
    for child in node.children:
        if child.type == "identifier":
            _c_add_global(builder, child.text.decode("utf-8"))
        elif child.type in ("init_declarator", "array_declarator"):
            for cc in child.children:
                if cc.type == "identifier":
                    _c_add_global(builder, cc.text.decode("utf-8"))


def _c_add_block(builder, node):
    # args carry the structural fact (in_block); the engine maps it to the
    # deletion text ("" inside a block, ";" for an unbraced control body).
    in_block = node.parent is not None and node.parent.type in (
        "compound_statement", "translation_unit", "declaration_list")
    name = f"{node.type}@{node.start_byte}:{node.end_byte}"
    block = DeclarationNode(name, node.type, builder.current_function,
                            (node.start_byte, node.end_byte, in_block))
    builder.graph.add_node(block)
    if builder.current_function is not None:
        builder.graph.add_edge(builder.current_function, block, label="block")


def _c_initializer_list(builder, node):
    if node.parent is not None and node.parent.type == "initializer_list":
        return
    name = f"init@{node.start_byte}:{node.end_byte}"
    init = DeclarationNode(name, "initializer", builder.current_function,
                           (node.start_byte, node.end_byte))
    builder.graph.add_node(init)
    if builder.current_function is not None:
        builder.graph.add_edge(builder.current_function, init, label="init")


def _c_add_expression(builder, node):
    if node is None or node.type not in _C_EXPR_VALUE_TYPES:
        return
    if node.end_byte - node.start_byte < EXPRESSION_MIN_BYTES:
        return
    name = f"expr@{node.start_byte}:{node.end_byte}"
    expr = DeclarationNode(name, "expression", builder.current_function,
                           (node.start_byte, node.end_byte))
    builder.graph.add_node(expr)
    if builder.current_function is not None:
        builder.graph.add_edge(builder.current_function, expr, label="expr")


def _c_assignment_expression(builder, node):
    if any(c.type == "=" for c in node.children):
        _c_add_expression(builder, node.child_by_field_name("right"))


def _c_init_declarator(builder, node):
    _c_add_expression(builder, node.child_by_field_name("value"))


def _c_return_statement(builder, node):
    for c in node.children:
        if c.type not in ("return", ";", "comment"):
            _c_add_expression(builder, c)
            return


def _c_call_expression(builder, node):
    args = node.child_by_field_name("arguments")
    if args is None:
        return
    for a in args.children:
        _c_add_expression(builder, a)


def _c_struct_declaration_name(parent_node):
    for child in parent_node.children:
        if child.type == "identifier":
            return child.text.decode("utf-8")
        if child.type in ("init_declarator", "pointer_declarator", "array_declarator"):
            for cc in child.children:
                if cc.type == "identifier":
                    return cc.text.decode("utf-8")


def _c_struct_parameter_name(parent_node):
    function_declarator = parent_node.parent.parent
    for child in function_declarator.children:
        if child.type == "identifier":
            return child.text.decode("utf-8")


def _c_struct_function_name(parent_node):
    for child in parent_node.children:
        if child.type == "function_declarator":
            for cc in child.children:
                if cc.type == "identifier":
                    return cc.text.decode("utf-8")


def _c_struct_specifier(builder, node):
    parent_node = node.parent if len(node.children) < 3 else None
    for child in node.children:
        if child.type != "type_identifier":
            continue
        struct_name = child.text.decode("utf-8")
        struct_node = DeclarationNode(struct_name, "struct", None)
        if struct_name not in builder.structs:
            builder.structs[struct_name] = struct_node
            return builder.graph.add_node(struct_node)
        if parent_node is not None:
            name = None
            if parent_node.type == "declaration":
                name = _c_struct_declaration_name(parent_node)
            elif parent_node.type == "parameter_declaration":
                name = _c_struct_parameter_name(parent_node)
            elif parent_node.type == "function_definition":
                name = _c_struct_function_name(parent_node)
            if name and builder.symbols.get(name) is not None:
                builder.graph.add_edge(struct_node, builder.symbols[name],
                                       label="struct")
        return


C_SCHEMA = LanguageSchema(
    language="c",
    custom={
        "function_definition": _c_function_definition,
        "declaration": _c_declaration,
        "struct_specifier": _c_struct_specifier,
        "for_statement": _c_add_block,
        "if_statement": _c_add_block,
        "initializer_list": _c_initializer_list,
        "assignment_expression": _c_assignment_expression,
        "init_declarator": _c_init_declarator,
        "return_statement": _c_return_statement,
        "call_expression": _c_call_expression,
    },
    custom_exit={
        "function_definition": _c_exit_function_definition,
    },
)


# --------------------------------------------------------------------------- #
# Registry / entry points                                                      #
# --------------------------------------------------------------------------- #

class SolidityGraphBuilder(GraphBuilder):
    SCHEMA = SOLIDITY


class JavaGraphBuilder(GraphBuilder):
    SCHEMA = JAVA


class CGraphBuilder(GraphBuilder):
    SCHEMA = C_SCHEMA

    def __init__(self) -> None:
        super().__init__()
        self.symbols: Dict[str, DeclarationNode] = {}
        self.structs: Dict[str, DeclarationNode] = {}


GRAPH_BUILDERS = {
    "solidity": SolidityGraphBuilder,
    "c": CGraphBuilder,
    "java": JavaGraphBuilder,
}


def get_graph_builder(language: str) -> type[GraphBuilder]:
    builder = GRAPH_BUILDERS.get(language)
    if builder is None:
        raise Exception(
            f"Graph builder for language '{language}' was not found")
    return builder


def build_graph_from_file(file_path: str, language: str) -> nx.DiGraph:
    builder = get_graph_builder(language)
    return builder().build_graph(file_path)
