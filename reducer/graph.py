import networkx as nx
from typing import NamedTuple, List, Any, Optional

from reducer import parsers

# Only track BIG arithmetic/logical expressions as collapse candidates — small
# subexpressions are cheap for Perses and probing them is wasted oracle calls.
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
        else:
            return node_name

    def __repr__(self) -> str:
        return self.__str__()


class GraphBuilder(parsers.TreeTraversal):
    LANGUAGE: Optional[str] = None

    def __init__(self) -> None:
        self.graph = nx.DiGraph()
        self.declaration_stack: List[DeclarationNode] = []

    def peek_declaration(self) -> Optional[DeclarationNode]:
        if not self.declaration_stack:
            return None
        return self.declaration_stack[-1]

    def push_declaration(self, node: DeclarationNode) -> None:
        self.declaration_stack.append(node)

    def pop_declaration(self) -> Optional[DeclarationNode]:
        if not self.declaration_stack:
            return None
        return self.declaration_stack.pop()

    def build_graph(self, source_file: str) -> nx.DiGraph:
        assert self.LANGUAGE is not None, "LANGUAGE must be set in subclasses"
        tree = parsers.parse(source_file, self.LANGUAGE)
        root_node = tree.root_node
        self.traverse_node(root_node)
        self.finalize_graph()
        return self.graph

    def finalize_graph(self) -> None:
        """Hook for post-traversal edge wiring (e.g. type-use dependencies)."""
        pass


class SolidityGraphBuilder(GraphBuilder):
    LANGUAGE: str = "solidity"

    def __init__(self) -> None:
        super().__init__()
        self.function_counter = 0
        self.state_variable_counter = 0
        self.local_variable_counter = 0
        self.contracts: dict[str, DeclarationNode] = {}
        # (declaration node, referenced user-type names) pending `uses-type` edges
        self.pending_type_uses: list = []

    def visit_default(self, node):
        pass

    def exit_default(self, node):
        pass

    def visit_contract_declaration(self, node):
        contract_name = parsers.declaration_name(node)
        contract_node = DeclarationNode(contract_name, "contract", None)
        self.graph.add_node(contract_node)
        self.push_declaration(contract_node)
        self.contracts[contract_node.name] = contract_node

        for child in node.children:
            if child.type == "inheritance_specifier":
                parent_name = child.text.decode("utf-8")
                if parent_name != contract_name:
                    parent_node = self.contracts[parent_name]
                    self.graph.add_edge(parent_node, contract_node,
                                        label="inherits")

    def exit_contract_declaration(self, ctx):
        self.pop_declaration()

    def visit_function_definition(self, node):
        func_name = parsers.declaration_name(node)
        signature = parsers.parameter_signature(node)
        parent_node = self.peek_declaration()
        # Identity = (name, "function", enclosing-contract, parameter signature),
        # so same-named functions in different contracts (or overloads) are
        # distinct, removable nodes.
        func_node = DeclarationNode(func_name, "function", parent_node, signature)
        self.graph.add_node(func_node)
        self.push_declaration(func_node)
        self.current_function = func_node  # Set the current function context
        if parent_node is not None:
            self.graph.add_edge(parent_node, func_node, label="def")

    def exit_function_definition(self, node):
        self.pop_declaration()

    def visit_event_definition(self, node):
        event_name = parsers.declaration_name(node)
        parent_node = self.peek_declaration()
        event_node = DeclarationNode(event_name, "event", parent_node)
        self.graph.add_node(event_node)
        if parent_node is not None:
            self.graph.add_edge(parent_node, event_node, label='def')

    def visit_modifier_definition(self, node):
        modifier_name = parsers.declaration_name(node)
        parent_node = self.peek_declaration()
        modifier_node = DeclarationNode(modifier_name, "modifier", parent_node)
        self.graph.add_node(modifier_node)
        if parent_node is not None:
            self.graph.add_edge(parent_node, modifier_node, label='def')

    def visit_struct_declaration(self, node):
        struct_name = parsers.declaration_name(node)
        parent_node = self.peek_declaration()
        struct_node = DeclarationNode(struct_name, "struct", parent_node)
        self.graph.add_node(struct_node)
        self.push_declaration(struct_node)
        if parent_node is not None:
            self.graph.add_edge(parent_node, struct_node, label='def')

    def exit_struct_declaration(self, node):
        self.pop_declaration()

    def visit_state_variable_declaration(self, node):
        var_name = parsers.declaration_name(node)
        parent_node = self.peek_declaration()
        var_node = DeclarationNode(var_name, "state_var", parent_node)
        self.graph.add_node(var_node)
        if parent_node is not None:
            self.graph.add_edge(parent_node, var_node, label='def')
        self.pending_type_uses.append(
            (var_node, parsers.type_reference_names(node)))

    def visit_variable_declaration(self, node):
        var_name = parsers.declaration_name(node)
        parent_node = self.peek_declaration()
        var_node = DeclarationNode(var_name, "var", parent_node)
        self.graph.add_node(var_node)
        if parent_node is not None:
            self.graph.add_edge(parent_node, var_node, label='def')
        self.pending_type_uses.append(
            (var_node, parsers.type_reference_names(node)))

    def finalize_graph(self) -> None:
        """Add ``uses-type`` edges: struct/contract -> declaration typed by it,
        so removing a type cascades (Interesting.update_graph + the remover's
        type-use closure) to the state vars / locals that use it."""
        type_nodes = {n.name: n for n in self.graph.nodes
                      if n.node_type in ("contract", "struct")}
        for var_node, type_names in self.pending_type_uses:
            for type_name in type_names:
                target = type_nodes.get(type_name)
                if target is not None and target is not var_node:
                    self.graph.add_edge(target, var_node, label="uses-type")

    def get_node_visitor(self, node):
        visitors = {
            "contract_declaration": self.visit_contract_declaration,
            "interface_declaration": self.visit_contract_declaration,
            "function_definition": self.visit_function_definition,
            "modifier_definition": self.visit_modifier_definition,
            "event_definition": self.visit_event_definition,
            "struct_declaration": self.visit_struct_declaration,
            "state_variable_declaration": self.visit_state_variable_declaration,
            "variable_declaration": self.visit_variable_declaration,
        }
        return visitors.get(node.type, self.visit_default)

    def get_node_exit(self, node):
        exit_funcs = {
            "contract_declaration": self.exit_contract_declaration,
            "interface_declaration": self.exit_contract_declaration,
            "function_definition": self.exit_function_definition,
            "struct_declaration": self.exit_struct_declaration,
        }
        return exit_funcs.get(node.type, self.exit_default)


class CGraphBuilder(GraphBuilder):
    LANGUAGE: str = "c"

    def __init__(self) -> None:
        super().__init__()
        self.function_counter = 0
        self.state_variable_counter = 0
        self.local_variable_counter = 0
        self.current_function: Optional[DeclarationNode] = None
        self.structs: dict[str, DeclarationNode] = {}
        self.declarations: dict[str, DeclarationNode] = {}

    def visit_default(self, node):
        pass

    def exit_default(self, node):
        pass

    def add_function_declaration_node(self, node):
        func_name = node.text.decode("utf-8")
        parent_node = self.peek_declaration()
        func_node = DeclarationNode(func_name, "function", parent_node)
        self.graph.add_node(func_node)
        self.push_declaration(func_node)
        self.declarations[func_name] = func_node
        self.current_function = func_node  # Set the current function context
        if parent_node is not None:
            self.graph.add_edge(parent_node, func_node, label="def")

    def visit_function_definition(self, node):
        for child in node.children:
            if child.type == "function_declarator":
                for child_child in child.children:
                    if child_child.type == "identifier":
                        self.add_function_declaration_node(child_child)
                        break
                    if child_child.type == "parenthesized_declarator":
                        for child_child_child in child_child.children:
                            if child_child_child.type == "identifier":
                                self.add_function_declaration_node(child_child_child)
                                break

    def exit_function_definition(self, node):
        self.pop_declaration()
        self.current_function = None

    def add_global_variable(self, var_name):
        if self.current_function:
            var_node = DeclarationNode(var_name, "global_variable", self.current_function)
            self.graph.add_node(var_node)
            self.declarations[var_name] = var_node
            self.push_declaration(var_node)
            if self.current_function is not None:
                self.graph.add_edge(self.current_function, var_node, label="var")
        else:
            parent_node = self.peek_declaration()
            var_node = DeclarationNode(var_name, "global_variable", parent_node)
            self.graph.add_node(var_node)
            self.declarations[var_name] = var_node
            self.push_declaration(var_node)
            if parent_node is not None:
                self.graph.add_edge(parent_node, var_node, label="var")

    def visit_declaration(self, node):
        global_variable = False
        for child in node.children:
            if (
                child.type == "storage_class_specifier"
                and child.text.decode("utf-8") == "static"
            ):
                global_variable = True
                break
        if global_variable:
            # A `static` declaration whose declarator is a function_declarator is
            # a prototype, not a data global; its name collides with the function
            # so don't emit it as a removable global_variable node.
            if any(c.type == "function_declarator" for c in node.children):
                return
            for child in node.children:
                if child.type == "identifier":
                    var_name = child.text.decode("utf-8")
                    self.add_global_variable(var_name)
                elif child.type in [
                    "init_declarator", "array_declarator"
                ]:
                    for child_child in child.children:
                        if child_child.type == "identifier":
                            var_name = child_child.text.decode("utf-8")
                            self.add_global_variable(var_name)

    def exit_declaration(self, node):
        pass

    def _add_block_node(self, node):
        # Identity = the block's byte range in the CURRENT source (args), not a
        # line number; the probe rebuilds the graph each round so offsets stay
        # fresh. repl = "" when the block sits in a real block, else ";" so
        # deleting an unbraced control body leaves a valid empty statement.
        repl = "" if (node.parent is not None and node.parent.type in (
            "compound_statement", "translation_unit", "declaration_list")
        ) else ";"
        name = f"{node.type}@{node.start_byte}:{node.end_byte}"
        block = DeclarationNode(name, node.type, self.current_function,
                                (node.start_byte, node.end_byte, repl))
        self.graph.add_node(block)
        if self.current_function is not None:
            self.graph.add_edge(self.current_function, block, label="block")

    def visit_for_statement(self, node):
        self._add_block_node(node)

    def exit_for_statement(self, node):
        pass

    def visit_if_statement(self, node):
        self._add_block_node(node)

    def exit_if_statement(self, node):
        pass

    def visit_initializer_list(self, node):
        # Value simplification: an aggregate initializer `{...}` collapses to
        # `{0}` in one shot (Perses reduces it element by element). Only the
        # OUTERMOST list is a node; collapsing it subsumes any nested lists.
        if node.parent is not None and node.parent.type == "initializer_list":
            return
        name = f"init@{node.start_byte}:{node.end_byte}"
        init = DeclarationNode(name, "initializer", self.current_function,
                               (node.start_byte, node.end_byte, "{0}"))
        self.graph.add_node(init)
        if self.current_function is not None:
            self.graph.add_edge(self.current_function, init, label="init")

    EXPR_VALUE_TYPES = ("binary_expression", "call_expression",
                        "conditional_expression", "parenthesized_expression")

    def _add_expression_node(self, node):
        # A large arithmetic/logical value collapses to a typed constant in one
        # call (Perses nibbles it operand by operand). Only WHOLE rvalues are
        # emitted (assignment RHS / return / init value), so the constant never
        # lands inside surrounding arithmetic (no new overflow UB).
        if node is None or node.type not in self.EXPR_VALUE_TYPES:
            return
        if node.end_byte - node.start_byte < EXPRESSION_MIN_BYTES:
            return
        name = f"expr@{node.start_byte}:{node.end_byte}"
        expr = DeclarationNode(name, "expression", self.current_function,
                               (node.start_byte, node.end_byte, "0xDEADBEEF"))
        self.graph.add_node(expr)
        if self.current_function is not None:
            self.graph.add_edge(self.current_function, expr, label="expr")

    def visit_assignment_expression(self, node):
        # plain `=` only; collapsing the RHS of `+=`/`*=`/... would inject
        # arithmetic on the constant and risk overflow UB.
        if any(c.type == "=" for c in node.children):
            self._add_expression_node(node.child_by_field_name("right"))

    def visit_init_declarator(self, node):
        self._add_expression_node(node.child_by_field_name("value"))

    def visit_return_statement(self, node):
        for c in node.children:
            if c.type not in ("return", ";", "comment"):
                self._add_expression_node(c)
                return

    def visit_call_expression(self, node):
        # Nested fallback: each call ARGUMENT is also a collapsible rvalue. When
        # an outer expression can't collapse (the bug is inside it), the probe
        # then collapses the non-bug argument subtrees; the overlap filter makes
        # an accepted outer collapse subsume these. Arguments are safe (a
        # constant passed to a function is a conversion, not arithmetic).
        args = node.child_by_field_name("arguments")
        if args is None:
            return
        for a in args.children:
            self._add_expression_node(a)

    def _handle_struct_declaration_parent(self, parent_node):
        for child in parent_node.children:
            if child.type == "identifier":
                return child.text.decode("utf-8")
            if child.type in ["init_declarator", "pointer_declarator", "array_declarator"]:
                for child_child in child.children:
                    if child_child.type == "identifier":
                        return child_child.text.decode("utf-8")

    def _handle_struct_parameter_declaration_parent(self, parent_node):
        function_declarator = parent_node.parent.parent
        for child in function_declarator.children:
            if child.type == "identifier":
                return child.text.decode("utf-8")

    def _handle_struct_function_definition_parent(self, parent_node):
        for child in parent_node.children:
            if child.type == "function_declarator":
                for child_child in child.children:
                    if child_child.type == "identifier":
                        return child_child.text.decode("utf-8")

    def visit_struct_specifier(self, node):
        parent_node = node.parent if len(node.children) < 3 else None
        for child in node.children:
            if child.type == "type_identifier":
                struct_name = child.text.decode("utf-8")
                struct_node = DeclarationNode(struct_name, "struct", None)
                if struct_name not in self.structs:
                    self.structs[struct_name] = struct_node
                    return self.graph.add_node(struct_node)
                if parent_node is not None:
                    declaration_name = None
                    if parent_node.type == "declaration":
                        declaration_name = self._handle_struct_declaration_parent(parent_node)
                    elif parent_node.type == "parameter_declaration":
                        declaration_name = self._handle_struct_parameter_declaration_parent(parent_node)
                    elif parent_node.type == "function_definition":
                        declaration_name = self._handle_struct_function_definition_parent(parent_node)
                    if declaration_name:
                        if self.declarations.get(declaration_name) is not None:
                            declaration = self.declarations.get(declaration_name)
                            self.graph.add_edge(struct_node, declaration, label="struct")
                    return

    def exit_struct_specifier(self, node):
        pass

    def get_node_visitor(self, node):
        visitors = {
            "function_definition": self.visit_function_definition,
            "declaration": self.visit_declaration,
            "struct_specifier": self.visit_struct_specifier,
            "for_statement": self.visit_for_statement,
            "if_statement": self.visit_if_statement,
            "initializer_list": self.visit_initializer_list,
            "assignment_expression": self.visit_assignment_expression,
            "init_declarator": self.visit_init_declarator,
            "return_statement": self.visit_return_statement,
            "call_expression": self.visit_call_expression,
        }
        return visitors.get(node.type, self.visit_default)

    def get_node_exit(self, node):
        exit_funcs = {
            "function_definition": self.exit_function_definition,
            "declaration": self.exit_declaration,
            "struct_specifier": self.exit_struct_specifier,
            "for_statement": self.exit_for_statement,
            "if_statement": self.exit_if_statement,
        }
        return exit_funcs.get(node.type, self.exit_default)


class JavaGraphBuilder(GraphBuilder):
    LANGUAGE = "java"

    def __init__(self):
        super().__init__()
        self.function_counter = 0
        self.state_variable_counter = 0
        self.local_variable_counter = 0
        self.declaration_stack: List[DeclarationNode] = []
        self.classes: dict = {}

    def visit_default(self, node):
        pass

    def exit_default(self, node):
        pass

    def visit_class_declaration(self, node):
        class_name = ""
        for n in node.children:
            if n.type == "identifier":
                class_name = n.text.decode("utf-8")
                break
        class_node = DeclarationNode(class_name, "class", None)
        self.graph.add_node(class_node)
        self.push_declaration(class_node)
        self.classes[class_node.name] = class_node

        for child in node.children:
            if child.type == "superclass":
                parent_name = child.text.decode("utf-8").split("extends ")[-1]
                if parent_name != class_name:
                    try:
                        parent_node = self.classes[parent_name]
                        self.graph.add_edge(parent_node, class_node,
                                            label="inherits")
                    except KeyError:
                        continue

    def exit_scope(self, node):
        """Pops the declaration pushed on entering a class/interface/method/
        constructor, so `peek_declaration` always reflects the true enclosing
        scope (and `parent` chains are correct)."""
        self.pop_declaration()

    def visit_function_definition(self, node):
        func_name = None
        for n in node.children:
            if n.type == "identifier":
                func_name = n.text.decode("utf-8")
                break

        if func_name is None:
            raise ValueError("Function name not found in node in visit_function_definition")
        function_args = None
        for child in node.children:
            if child.type == "formal_parameters":
                function_args = child.text.decode("utf-8")
                break

        parent_node = self.peek_declaration()
        if node.type == "constructor_declaration":
            func_node = DeclarationNode(func_name, "constructor", parent_node, function_args)
        else:
            func_node = DeclarationNode(func_name, "function", parent_node, function_args)
        self.graph.add_node(func_node)
        self.push_declaration(func_node)
        self.current_function = func_node
        if parent_node is not None:
            self.graph.add_edge(parent_node, func_node, label="def")

    def visit_field_declaration(self, node):
        for child in node.children:
            if child.type == "variable_declarator":
                var_name_node = child.child_by_field_name("name")
                if var_name_node:
                    field_name = var_name_node.text.decode("utf-8")
                    parent_node = self.peek_declaration()
                    field_node = DeclarationNode(field_name, "field", parent_node)
                    self.graph.add_node(field_node)
                    if parent_node is not None:
                        self.graph.add_edge(parent_node, field_node, label="def")

    def visit_local_variable_declaration(self, node):

        parent = self.peek_declaration()

        type_node = node.child_by_field_name("type")

        for decl in node.children:
            if decl.type == "variable_declarator":
                name_node = decl.child_by_field_name("name")
                if not name_node:
                    continue
                var_name = name_node.text.decode("utf-8")
                local_node = DeclarationNode(var_name, "local_variable", parent)
                self.graph.add_node(local_node)
                if parent is not None:
                    self.graph.add_edge(parent, local_node, label="def")

    def exit_function_definition(self, node):
        self.pop_declaration()

    def visit_event_definition(self, node):
        event_name = node.text.decode("utf-8")
        parent_node = self.peek_declaration()
        event_node = DeclarationNode(event_name, "event", parent_node)
        self.graph.add_node(event_node)
        if parent_node is not None:
            self.graph.add_edge(parent_node, event_node, label='def')

    def get_node_visitor(self, node):
        visitors = {
            "class_declaration": self.visit_class_declaration,
            "interface_declaration": self.visit_class_declaration,
            "method_declaration": self.visit_function_definition,
            "event_definition": self.visit_event_definition,
            "field_declaration": self.visit_field_declaration,
            "constructor_declaration": self.visit_function_definition,
            "local_variable_declaration": self.visit_local_variable_declaration,
        }
        return visitors.get(node.type, self.visit_default)

    def get_node_exit(self, node):
        # Pop every scope we pushed on enter, so nesting/parent is correct. The
        # node types must match the Java grammar (method/class/...), not the
        # Solidity ones -- keying them wrong leaves the stack growing forever.
        exit_funcs = {
            "class_declaration": self.exit_scope,
            "interface_declaration": self.exit_scope,
            "method_declaration": self.exit_scope,
            "constructor_declaration": self.exit_scope,
        }
        return exit_funcs.get(node.type, self.exit_default)


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
