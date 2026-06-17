import re
import traceback
from abc import abstractmethod
from typing import Any

import networkx as nx

from reducer import parsers


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


class SolidityDeclarationRemoval(ASTRemoval):
    LANGUAGE = "solidity"
    # Single shared placeholder type that replaces a removed struct wherever it
    # is still referenced in a position deletion cannot touch (a parameter /
    # return type). See `_placeholder_retype_edits`.
    PLACEHOLDER_STRUCT = "__S"

    def __init__(self, content, graph):
        super().__init__(content, graph)
        self.removed_nodes = []
        self.removed_ranges = []
        self.contract_scope = []

    def visit_default(self, node):
        pass

    def exit_default(self, node):
        pass

    def get_node_visitor(self, node):
        visitors = {
            "contract_declaration": self.visit_contract_declaration,
            "interface_declaration": self.visit_contract_declaration,
            "function_definition": self.visit_function_definition,
            "call_expression": self.visit_call_expression,
            "modifier_definition": self.visit_modifier_definition,
            "modifier_invocation": self.visit_modifier_invocation,
            "struct_declaration": self.visit_struct_definition,
            "state_variable_declaration": self.visit_state_variable_declaration,
            "event_definition": self.visit_event_definition,
            "emit_statement": self.visit_emit_statement,
            "inheritance_specifier": self.visit_inheritance_specifier,
            "using_directive": self.visit_using_directive,
            # Use-site cleanup: drop statements that reference a removed
            # declaration (state var / local var / struct), keeping the program
            # free of dangling references.
            "expression_statement": self.visit_use_site_statement,
            "return_statement": self.visit_use_site_statement,
            "variable_declaration_statement": self.visit_use_site_statement,
            # A control-flow statement whose *header* (condition / loop
            # init+update) references a removed value can't survive -- its body
            # statements are cleaned individually by the recursion, but a
            # dangling condition would not compile, so drop the whole statement.
            "if_statement": self.visit_use_site_control_flow,
            "for_statement": self.visit_use_site_control_flow,
            "while_statement": self.visit_use_site_control_flow,
            "do_while_statement": self.visit_use_site_control_flow,
        }
        return visitors.get(node.type, self.visit_default)

    def get_node_exit(self, node):
        exit_funcs = {
            "contract_declaration": self.exit_contract_declaration,
            "interface_declaration": self.exit_contract_declaration,
        }
        return exit_funcs.get(node.type, self.exit_default)

    def _current_contract(self):
        return self.contract_scope[-1] if self.contract_scope else None

    def _is_selected(self, name, node_type, signature=None):
        """True if the delta-debugger selected *this* declaration for removal.

        Declarations are matched by a position-independent identity --
        ``(enclosing contract, name, node type[, parameter signature])`` -- so
        same-named declarations in different contracts (and overloads) are
        addressed independently. Matching by bare name instead would couple
        them and prevent 1-minimal reductions; matching by byte offset would
        break as the reducer mutates the source.
        """
        contract = self._current_contract()
        for n in self.nodes_to_remove:
            if n.node_type != node_type or n.name != name:
                continue
            if getattr(n.parent, "name", None) != contract:
                continue
            if node_type == "function" and n.args != signature:
                continue
            return True
        return False

    def _name_fully_removed(self, name):
        """True if every function declaration with this name is being removed.

        A call is only dangling when no same-named function survives, so we
        strip calls only then -- removing one of several same-named functions
        never deletes calls bound to the survivors. A function is removed when it
        is selected directly or its enclosing contract is being removed.
        """
        decls = [n for n in self.graph.nodes
                 if n.node_type == "function" and n.name == name]
        return bool(decls) and all(
            n in self.nodes_to_remove
            or getattr(n.parent, "name", None) in self.removed_contracts
            for n in decls
        )

    def _mark(self, node):
        if node is not None and node not in self.removed_nodes:
            self.removed_nodes.append(node)

    def visit_contract_declaration(self, node):
        """Tracks the enclosing contract; removes the whole block if selected."""
        name = parsers.declaration_name(node)
        self.contract_scope.append(name)
        if name in self.removed_contracts:
            self._mark(node)

    def exit_contract_declaration(self, node):
        if self.contract_scope:
            self.contract_scope.pop()

    def visit_function_definition(self, node):
        """Collects the specific function selected for removal."""
        name = parsers.declaration_name(node)
        signature = parsers.parameter_signature(node)
        if self._is_selected(name, "function", signature):
            self.removed_nodes.append(node)

    def visit_modifier_definition(self, node):
        """Collects modifier nodes selected for removal."""
        if self._is_selected(parsers.declaration_name(node), "modifier"):
            self.removed_nodes.append(node)

    def visit_struct_definition(self, node):
        """Collects struct nodes selected for removal."""
        if self._is_selected(parsers.declaration_name(node), "struct"):
            self.removed_nodes.append(node)

    # Note: local variables (node_type "var") are handled at the statement level
    # by `visit_use_site_statement` -- deleting the bare `variable_declaration`
    # would leave a dangling `= expr;`, so there is deliberately no visitor for it.

    def visit_state_variable_declaration(self, node):
        """Collects state variable nodes selected for removal."""
        if self._is_selected(parsers.declaration_name(node), "state_var"):
            self.removed_nodes.append(node)

    def visit_event_definition(self, node):
        """Collects event nodes selected for removal."""
        if self._is_selected(parsers.declaration_name(node), "event"):
            self.removed_nodes.append(node)

    def _callee_name(self, node):
        """Name being called/emitted by a call_expression, or None for casts etc."""
        callee = node.children[0] if node.children else None
        if callee is None:
            return None
        inner = callee
        if callee.type == "expression" and callee.children:
            inner = callee.children[0]
        if inner.type == "identifier":
            return inner.text.decode("utf-8")
        if inner.type == "member_expression" and inner.children:
            return inner.children[-1].text.decode("utf-8")
        return None

    def _enclosing_statement(self, node):
        """Nearest enclosing statement node, so a removed call/emit takes its
        whole statement with it (avoids leaving a dangling reference)."""
        current = node
        while current is not None:
            if current.type.endswith("_statement"):
                return current
            current = current.parent
        return None

    def visit_call_expression(self, node):
        """Removes calls to removed functions and old-style event emits.

        Calls are only stripped when the callee no longer exists (a removed
        event, or a function with no surviving same-named declaration), so the
        result keeps no dangling references."""
        call_name = self._callee_name(node)
        if call_name is None:
            return
        if call_name in self.removed_events or self._name_fully_removed(call_name):
            self._mark(self._enclosing_statement(node) or node)

    def visit_emit_statement(self, node):
        """Removes an ``emit Event(...)`` statement when the event is removed
        (the 0.5+ syntax; pre-0.5 emits are plain calls handled above)."""
        for child in node.children:
            if child.type == "expression":
                inner = child.children[0] if child.children else None
                name = None
                if inner is not None and inner.type == "identifier":
                    name = inner.text.decode("utf-8")
                elif inner is not None and inner.type == "member_expression" \
                        and inner.children:
                    name = inner.children[-1].text.decode("utf-8")
                if name in self.removed_events:
                    self._mark(node)
                return

    def visit_modifier_invocation(self, node):
        """Removes a modifier usage (e.g. ``onlyOwner``) on a function when the
        modifier itself is being removed."""
        for child in node.children:
            if child.type == "identifier":
                if child.text.decode("utf-8") in self.removed_modifiers:
                    self._mark(node)
                return

    def visit_using_directive(self, node):
        """Removes ``using Lib for ...`` when ``Lib`` is a removed contract/library."""
        for child in node.children:
            if child.type in ("type_alias", "user_defined_type", "identifier"):
                if child.text.decode("utf-8") in self.removed_contracts:
                    self._mark(node)
                return

    @staticmethod
    def _inheritance_base(node):
        return node.text.decode("utf-8").split("(")[0].strip()

    def visit_inheritance_specifier(self, node):
        """Removes a base from ``contract X is A, B`` when the base is removed,
        cleaning up the ``is`` keyword / commas so the result stays valid."""
        if self._inheritance_base(node) not in self.removed_contracts:
            return
        parent = node.parent
        siblings = parent.children
        specs = [c for c in siblings if c.type == "inheritance_specifier"]
        surviving = [s for s in specs
                     if self._inheritance_base(s) not in self.removed_contracts]
        if not surviving:
            # remove the whole inheritance clause: `is A, B`
            is_kw = next((c for c in siblings if c.type == "is"), None)
            start = is_kw.start_byte if is_kw else specs[0].start_byte
            end = max(s.end_byte for s in specs)
            self.removed_ranges.append((start, end))
        else:
            # remove this base plus one adjacent comma
            idx = siblings.index(node)
            start, end = node.start_byte, node.end_byte
            if idx > 0 and siblings[idx - 1].type == ",":
                start = siblings[idx - 1].start_byte
            elif idx + 1 < len(siblings) and siblings[idx + 1].type == ",":
                end = siblings[idx + 1].end_byte
            self.removed_ranges.append((start, end))

    def _references_removed_value(self, node):
        """True if any identifier/type under ``node`` names a removed state var,
        local var, or struct."""
        stack = list(node.children)
        while stack:
            n = stack.pop()
            if n.type in ("identifier", "type_name", "user_defined_type"):
                if n.text.decode("utf-8") in self.removed_value_refs:
                    return True
            stack.extend(n.children)
        return False

    def visit_use_site_statement(self, node):
        """Removes a statement that uses (or, for a local variable, declares) a
        removed state var / local var / struct, so no dangling reference remains."""
        if self.removed_value_refs and self._references_removed_value(node):
            self._mark(node)

    def visit_use_site_control_flow(self, node):
        """Removes an ``if``/``for``/``while`` whose header references a removed
        value. Only the header (condition, loop init/update -- every child except
        the ``body``) is checked: body statements are cleaned individually by the
        recursion, but a removed value in the condition leaves no valid statement,
        so the whole construct is dropped (e.g. ``for (..; i < removed.length; ..)``)."""
        if not self.removed_value_refs:
            return
        body = node.child_by_field_name("body")
        body_id = body.id if body is not None else None
        if any(child.id != body_id and self._references_removed_value(child)
               for child in node.children):
            self._mark(node)

    def _expand_dead_locals(self, tree):
        """Fixpoint over local declarations: a local whose initializer references
        an already-removed value is itself dead, so its name joins
        ``removed_value_refs`` and its later uses (including loop headers) are
        stripped too. Without this, removing e.g. a state var ``xs`` leaves a
        dangling ``for (..; i < n; ..)`` where ``uint n = xs.length;`` was cut."""
        changed = True
        while changed:
            changed = False
            stack = [tree.root_node]
            while stack:
                n = stack.pop()
                if n.type == "variable_declaration_statement":
                    decl = next((c for c in n.children
                                 if c.type == "variable_declaration"), None)
                    name = parsers.declaration_name(decl) if decl else None
                    if (name and name not in self.removed_value_refs
                            and self._references_removed_value(n)):
                        self.removed_value_refs.add(name)
                        changed = True
                stack.extend(n.children)

    def remove_nodes(self, nodes_to_remove: set, mode: str) -> str:
        """
        Removes nodes from Solidity source code.
        
        Args:
            nodes_to_remove: Set of nodes to be removed
            mode: Strategy for handling nodes (currently 'removal' is the primary mode for Solidity)
        
        Returns:
            Modified source code as a string with nodes removed
        """
        if mode not in ["removal"]:
            raise ValueError(f"Unknown mode: {mode}. Must be 'removal'")

        parser = parsers.get_parser(self.LANGUAGE)
        tree = parser.parse(self.content.encode("utf-8"))
        self.nodes_to_remove = nodes_to_remove
        self.removed_nodes = []
        self.removed_ranges = []
        # Names of declarations being removed, used to also strip their references
        # (inheritance, modifier usages, emits, library `using`s) so the reduced
        # program keeps no dangling references.
        self.removed_contracts = {n.name for n in nodes_to_remove
                                  if n.node_type == "contract"}
        self.removed_events = {n.name for n in nodes_to_remove
                               if n.node_type == "event"}
        self.removed_modifiers = {n.name for n in nodes_to_remove
                                  if n.node_type == "modifier"}
        # State vars / local vars / structs whose *uses* must also be stripped.
        self.removed_value_refs = {n.name for n in nodes_to_remove
                                   if n.node_type in ("state_var", "var", "struct")}
        # Removing a contract takes all its members with it, so references to
        # those members elsewhere (inherited modifier usages, emits, calls,
        # state-var/struct uses) must be cleaned up too.
        for n in self.graph.nodes:
            if getattr(n.parent, "name", None) in self.removed_contracts:
                if n.node_type == "modifier":
                    self.removed_modifiers.add(n.name)
                elif n.node_type == "event":
                    self.removed_events.add(n.name)
                elif n.node_type in ("state_var", "var", "struct"):
                    self.removed_value_refs.add(n.name)

        # Type-use cascade (option B): removing a contract/struct also removes
        # the declarations typed by it (followed via `uses-type` edges) and their
        # uses, so no dangling type reference is left behind.
        expanded = set(self.nodes_to_remove)
        work = [n for n in self.nodes_to_remove
                if n.node_type in ("contract", "struct")]
        seen = set(work)
        while work:
            type_node = work.pop()
            if type_node not in self.graph:
                continue
            for _, dependent, data in self.graph.out_edges(type_node, data=True):
                if data.get("label") != "uses-type" or dependent in seen:
                    continue
                seen.add(dependent)
                expanded.add(dependent)
                self.removed_value_refs.add(dependent.name)
                if dependent.node_type == "struct":
                    work.append(dependent)
        self.nodes_to_remove = expanded

        # Transitively mark locals that become dead once the above values are
        # removed, so their uses (e.g. in loop conditions) are stripped too.
        self._expand_dead_locals(tree)

        self.traverse_node(tree.root_node)

        # Collect byte ranges to delete: whole removed nodes + explicit ranges
        # (e.g. an `is Base` clause), then merge overlapping/nested ranges so a
        # removed contract and its inner members produce a single clean edit.
        ranges = [(n.start_byte, n.end_byte) for n in self.removed_nodes]
        ranges.extend(self.removed_ranges)
        ranges.sort()
        merged = []
        for start, end in ranges:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])

        # Build the edit list: deletions (replace with nothing) plus the
        # type-use placeholder retyping. A removed struct still referenced in a
        # position deletion cannot touch (a parameter / return type) has that
        # reference rewritten to a single placeholder `struct __S` seeded with
        # the struct's members, so the program stays valid -- the fallback after
        # deletion. References inside a deleted range are skipped.
        edits = [(s, e, b"") for s, e in merged]
        edits += self._placeholder_retype_edits(tree, merged)
        if not edits:
            return self.content

        # Apply edits right-to-left over the exact bytes parsed above, NOT
        # tree.root_node.text: when the source has leading/trailing whitespace
        # (e.g. a leading comment stripped to blank lines), tree-sitter's root
        # node starts after it, so root_node.text is shorter than the input and
        # every absolute start_byte/end_byte would be misaligned -> corrupt cuts.
        source = self.content.encode("utf-8")
        for start, end, repl in sorted(edits, key=lambda e: e[0], reverse=True):
            source = source[:start] + repl + source[end:]
        return remove_empty_lines(source.decode("utf-8"))

    # --- type-use placeholder retyping (fallback after deletion) -------------

    def _enclosing_contract_node(self, node):
        cur = node.parent
        while cur is not None:
            if cur.type in ("contract_declaration", "interface_declaration"):
                return cur
            cur = cur.parent
        return None

    @staticmethod
    def _struct_members(struct_node):
        """``(field name, field text)`` for each member of a struct node."""
        body = next((c for c in struct_node.children
                     if c.type == "struct_body"), None)
        if body is None:
            return []
        return [(parsers.declaration_name(m), m.text.decode("utf-8"))
                for m in body.children if m.type == "struct_member"]

    def _retype_text(self, text, names):
        """Rewrite whole-word occurrences of removed struct ``names`` (e.g. in a
        copied member's type) to the placeholder, so nested uses stay valid."""
        for name in names:
            text = re.sub(rf"\b{re.escape(name)}\b",
                          self.PLACEHOLDER_STRUCT, text)
        return text

    def _placeholder_struct_decl(self, members, names):
        """A one-line ``struct __S { ... }`` from member declarations, deduped by
        field name (copy-all; the field text already carries its ``;``)."""
        seen, fields = set(), []
        for fname, text in members:
            if fname in seen:
                continue
            seen.add(fname)
            fields.append(self._retype_text(text, names))
        return f"struct {self.PLACEHOLDER_STRUCT} {{ {' '.join(fields)} }}"

    def _placeholder_insert_pos(self, tree, contract_node):
        """Where to inject a placeholder struct: just inside the enclosing
        contract body (a struct must live in a contract in <0.6 Solidity), or
        after the pragma for a file-level reference."""
        if contract_node is not None:
            body = next((c for c in contract_node.children
                         if c.type == "contract_body"), None)
            if body is not None:
                brace = next((c for c in body.children if c.type == "{"), None)
                if brace is not None:
                    return brace.end_byte
            return contract_node.end_byte
        pragma = next((c for c in tree.root_node.children
                       if c.type == "pragma_directive"), None)
        return pragma.end_byte if pragma is not None else 0

    def _placeholder_retype_edits(self, tree, deleted_ranges):
        """Edits that retype surviving references to removed structs to a
        per-contract placeholder ``struct __S``.

        For each removed struct still referenced as a *type* outside any deleted
        range (e.g. a function parameter), the reference is rewritten to ``__S``
        and a ``struct __S`` carrying that struct's members is injected into (or
        merged with an existing one in) the enclosing contract, so the reduced
        program still type-checks. References that deletion already removes, and
        references inside a placeholder we are about to rebuild, are left alone.
        """
        removed = {n.name for n in self.nodes_to_remove
                   if n.node_type == "struct"
                   and n.name != self.PLACEHOLDER_STRUCT}
        if not removed:
            return []

        # Members of each removed struct (only structs we can copy are retyped)
        # and any pre-existing placeholder to merge into, keyed by contract id.
        members_by_struct = {}
        existing = {}                 # enclosing contract id (or None) -> __S node
        stack = [tree.root_node]
        while stack:
            n = stack.pop()
            if n.type == "struct_declaration":
                sname = parsers.declaration_name(n)
                if sname in removed:
                    members_by_struct[sname] = self._struct_members(n)
                elif sname == self.PLACEHOLDER_STRUCT:
                    cnode = self._enclosing_contract_node(n)
                    existing[id(cnode) if cnode else None] = n
            stack.extend(n.children)
        retypable = {s for s in removed if members_by_struct.get(s)}
        if not retypable:
            return []

        existing_ranges = [(e.start_byte, e.end_byte) for e in existing.values()]

        def covered(start, end, ranges):
            return any(rs <= start and end <= re_ for rs, re_ in ranges)

        edits = []
        seeds = {}                    # contract id -> (contract node, {struct names})
        stack = [tree.root_node]
        while stack:
            n = stack.pop()
            stack.extend(n.children)
            if n.type != "user_defined_type":
                continue
            name = n.text.decode("utf-8")
            if name not in retypable:
                continue
            if covered(n.start_byte, n.end_byte, deleted_ranges):
                continue              # deletion already removes this reference
            if covered(n.start_byte, n.end_byte, existing_ranges):
                continue              # inside a placeholder we will rebuild below
            edits.append((n.start_byte, n.end_byte,
                          self.PLACEHOLDER_STRUCT.encode("utf-8")))
            cnode = self._enclosing_contract_node(n)
            key = id(cnode) if cnode is not None else None
            seeds.setdefault(key, (cnode, set()))[1].add(name)

        if not edits:
            return []

        # Inject (or rebuild) one placeholder per contract that gained a use.
        for key, (cnode, names) in seeds.items():
            members = []
            if key in existing:
                members.extend(self._struct_members(existing[key]))
            for s in names:
                members.extend(members_by_struct[s])
            decl = self._placeholder_struct_decl(members, retypable)
            if key in existing:
                e = existing[key]
                edits.append((e.start_byte, e.end_byte, decl.encode("utf-8")))
            else:
                pos = self._placeholder_insert_pos(tree, cnode)
                edits.append((pos, pos, f"\n    {decl}\n".encode("utf-8")))
        return edits

    # --- inheritance-chain simplification (flattening) -----------------------

    @staticmethod
    def _contract_body(contract_node):
        return next((c for c in contract_node.children
                     if c.type == "contract_body"), None)

    @staticmethod
    def _is_constructor(member, contract_name):
        # 0.5+ uses `constructor`; <=0.4.x names the constructor after the contract.
        if member.type == "constructor_definition":
            return True
        return (member.type == "function_definition"
                and parsers.declaration_name(member) == contract_name)

    def _inheritance_clause_edit(self, child, base_name, replacement_names):
        """Edit (start, end, text) that drops ``base_name`` from ``child``'s
        inheritance list, substituting the base's own parents (rewiring the
        chain) and tidying the ``is`` keyword / commas."""
        siblings = child.children
        specs = [c for c in siblings if c.type == "inheritance_specifier"]
        target = next((s for s in specs
                       if self._inheritance_base(s) == base_name), None)
        if target is None:
            return None
        surviving = [self._inheritance_base(s) for s in specs
                     if s is not target]
        surviving += [n for n in replacement_names if n not in surviving]
        if not surviving:
            is_kw = next((c for c in siblings if c.type == "is"), None)
            start = is_kw.start_byte if is_kw else specs[0].start_byte
            end = max(s.end_byte for s in specs)
            return (start, end, "")
        idx = siblings.index(target)
        start, end = target.start_byte, target.end_byte
        if idx > 0 and siblings[idx - 1].type == ",":
            start = siblings[idx - 1].start_byte
        elif idx + 1 < len(siblings) and siblings[idx + 1].type == ",":
            end = siblings[idx + 1].end_byte
        # if the base itself had parents, splice them in where it was
        extra = [n for n in replacement_names
                 if n not in {self._inheritance_base(s) for s in specs}]
        return (start, end, (", ".join(extra)) if extra else "")

    def flatten_inheritance(self, nodes_to_remove: set) -> str:
        """Eliminate base contracts by promoting their members into the children
        that inherit them, then deleting the base and rewiring the chain.

        This de-shares inherited members so the base can be removed even when a
        child still uses an inherited field/method -- a semantic-aware bulk
        reduction. The result is validated by the property check, so unsound
        cases (e.g. ``super``/diamond) are simply rejected.
        """
        parser = parsers.get_parser(self.LANGUAGE)
        tree = parser.parse(self.content.encode("utf-8"))
        contracts = {}
        stack = [tree.root_node]
        while stack:
            n = stack.pop()
            if n.type in ("contract_declaration", "interface_declaration"):
                contracts[parsers.declaration_name(n)] = n
            stack.extend(n.children)

        flatten_names = {n.name for n in nodes_to_remove
                         if n.node_type == "contract" and n.name in contracts}
        edits = []        # (start, end, replacement_text); start==end => insert
        flattened = False
        for base_name in flatten_names:
            base = contracts[base_name]
            body = self._contract_body(base)
            if body is None:
                continue
            members = [c for c in body.children
                       if c.type not in ("{", "}")
                       and not self._is_constructor(c, base_name)]
            # `super` would lose its target once the chain is broken -> skip.
            if any(b"super." in m.text for m in members):
                continue
            member_texts = [(parsers.declaration_name(m), m.text.decode("utf-8"))
                            for m in members]
            base_parents = [self._inheritance_base(s) for s in base.children
                            if s.type == "inheritance_specifier"]
            # direct children: contracts whose inheritance list names the base
            children = [c for name, c in contracts.items()
                        if name != base_name
                        and any(s.type == "inheritance_specifier"
                                and self._inheritance_base(s) == base_name
                                for s in c.children)]
            if not children:
                continue  # nothing inherits it; plain removal handles that
            for child in children:
                cbody = self._contract_body(child)
                if cbody is None:
                    continue
                existing = {parsers.declaration_name(c) for c in cbody.children
                            if c.type not in ("{", "}")}
                # skip members the child overrides (avoids duplicate definitions)
                to_add = [txt for nm, txt in member_texts if nm not in existing]
                if to_add:
                    close = [c for c in cbody.children if c.type == "}"][-1]
                    edits.append((close.start_byte, close.start_byte,
                                  "\n" + "\n\n".join(to_add) + "\n"))
                clause = self._inheritance_clause_edit(child, base_name, base_parents)
                if clause is not None:
                    edits.append(clause)
            edits.append((base.start_byte, base.end_byte, ""))
            flattened = True

        if not flattened:
            return self.content
        # Use the parsed source itself (not tree.root_node.text, which omits any
        # leading/trailing whitespace) so the absolute byte offsets in `edits`
        # stay aligned -- see remove_nodes for the detailed rationale.
        out = self.content
        for start, end, text in sorted(edits, key=lambda e: e[0], reverse=True):
            out = out[:start] + text + out[end:]
        return remove_empty_lines(out)


class CDeclarationRemoval(ASTRemoval):
    LANGUAGE = "c"

    def __init__(self, content, graph):
        super().__init__(content, graph)
        self.removed_nodes = []
        self.removed_declarations = []
        self.goto_statements = []
        self.replaced_assignment_declarations = []
        self.removed_nodes_with_types = {}
        self.constant_values = {
            'int': '0xDEADBEEF',
            'short': '0xDEAD',
            'long': '0xDEADBEEFDEADBEEF',
            'long long': '0xDEADBEEFDEADBEEF',
            'unsigned int': '0xDEADBEEFU',
            'unsigned short': '0xDEADU',
            'unsigned long': '0xDEADBEEFDEADBEEFUL',
            'unsigned long long': '0xDEADBEEFDEADBEEFULL',

            'char': "'X'",
            'unsigned char': "'X'",
            'signed char': "'X'",

            'float': '0xDEADBEEF',
            'double': '0xDEADBEEFDEADBEEF',
            'long double': '0xDEADBEEFDEADBEEF',

            'bool': 'false',
            '_Bool': 'false',

            'void*': 'NULL',
            None: 'NULL',
            'None': 'NULL',
            'void': 'NULL',
            'char*': 'NULL',
            'int*': 'NULL',
            'float*': 'NULL',
            'double*': 'NULL',

            'size_t': '0xDEADBEEF',
            'ssize_t': '0xDEADBEEF',
            'int8_t': '0xDE',
            'uint8_t': '0xDEU',
            'int16_t': '0xDEAD',
            'uint16_t': '0xDEADU',
            'int32_t': '0xDEADBEEF',
            'uint32_t': '0xDEADBEEFU',
            'int64_t': '0xDEADBEEFDEADBEEF',
            'uint64_t': '0xDEADBEEFDEADBEEFULL',

            'struct': '{}',

            'array': 'array[31000]',
}

    def visit_default(self, node):
        pass

    def exit_default(self, node):
        pass

    def get_node_visitor(self, node):
        visitors = {
            "function_definition": self.visit_function_definition,
            "expression_statement": self.visit_expression_statement,
            "call_expression": self.visit_call_expression,
            "goto_statement": self.visit_goto_statement,
            "labeled_statement": self.visit_labeled_statement,
            "declaration": self.visit_declaration,
            "if_statement": self.visit_if_statement,
            "for_statement": self.visit_for_statement,
            "struct_specifier": self.visit_struct_specifier,
            "identifier": self.visit_identifier,
        }
        if self.mode in ["replacement", "combination"]:
            visitors.update({
                "return_statement": self.visit_return_statement,
            })
        return visitors.get(node.type, self.visit_default)

    def get_node_exit(self, node):
        exit_funcs = {
        }
        return exit_funcs.get(node.type, self.exit_default)

    def _add_to_removed_nodes(self, node):
        if node is not None and node not in self.removed_nodes:
            self.removed_nodes.append(node)

    def _add_to_removed_declarations(self, declaration_name):
        if declaration_name not in self.removed_declarations:
            self.removed_declarations.append(declaration_name)

    def _add_declaration_to_removed_declarations(self, declaration_node):
        for declaration_child in declaration_node.children:
            if declaration_child.type == "identifier":
                decl_name = declaration_child.text.decode("utf-8")
                self._add_to_removed_declarations(decl_name)
            elif declaration_child.type in ["init_declarator", "array_declarator"]:
                self._add_declaration_to_removed_declarations(declaration_child)

    def _find_specific_parent_node(self, node, parent_node_type):
        if node is None:
            return
        if node.type == parent_node_type:
            return node
        else:
            return self._find_specific_parent_node(node.parent, parent_node_type)

    def _has_parent_node(self, node, parent_node):
        current = node.parent
        while current is not None:
            if current == parent_node:
                return True
            current = current.parent
        return False

    def _handle_function_definition_removal(self, child, node, node_type):
        child_name = child.text.decode("utf-8")
        if child_name not in self.removed_nodes_with_types:
            self.removed_nodes_with_types[child_name] = node_type
        if child_name in self.removed_declarations:
            return self._add_to_removed_nodes(node)
        for removal_node in self.functions_to_remove:
            if (removal_node.name == child_name
                and node not in self.removed_nodes):
                if child_name == "main":
                    # keep main function but remove code
                    for main_child in node.children:
                        if main_child.type == "compound_statement":
                            for main_code in main_child.children:
                                if main_code.type not in ["{", "}"]:
                                    self._add_to_removed_nodes(main_code)
                else:
                    return self._add_to_removed_nodes(node)

    def _get_node_type(self, child):
        if child.type in ["primitive_type", "sized_type_specifier"]:
            return child.text.decode("utf-8")
        if child.type == "struct_specifier":
            return "struct"
        return None

    def visit_function_definition(self, node):
        """Collects function definition nodes to be removed."""
        node_type = None
        for child in node.children:
            if not node_type:
                node_type = self._get_node_type(child)
            if child.type == "function_declarator":
                for child_child in child.children:
                    if child_child.type == "identifier":
                        return self._handle_function_definition_removal(child_child, node, node_type)
                    elif child_child.type == "parenthesized_declarator":
                        for child_child_child in child_child.children:
                            if child_child_child.type == "identifier":
                                return self._handle_function_definition_removal(child_child_child, node, node_type)

    def _find_expression_statement_removal_parent_node(self, node):
        removal_parent_node = self._find_specific_parent_node(node, "expression_statement")
        if removal_parent_node:
            return removal_parent_node

        removal_parent_node = self._find_specific_parent_node(node, "declaration")
        if removal_parent_node:
            self._add_declaration_to_removed_declarations(removal_parent_node)
            return removal_parent_node

        for parent_type in ["if_statement", "for_statement"]:
            removal_parent_node = self._find_specific_parent_node(node, parent_type)
            if removal_parent_node:
                return removal_parent_node

        return None

    def _handle_call_expression_argument_list(self, child, node, node_type):
        for child_child in child.children:
            if child_child.type == "identifier":
                variable_name = child_child.text.decode("utf-8")
                if variable_name in self.removed_declarations:
                    if self.mode == "replacement":
                        return self.replaced_assignment_declarations.append(
                            (node, self.removed_nodes_with_types[variable_name], None)
                        )
                    else:
                        removal_parent_node = self._find_expression_statement_removal_parent_node(node)
                        return self._add_to_removed_nodes(removal_parent_node)

    def _handle_call_expression_identifier(self, child, node, node_type):
        call_name = child.text.decode("utf-8")
        for removal_node in self.functions_to_remove:
            if removal_node.name == call_name:
                if self.mode == "replacement":
                    return self.replaced_assignment_declarations.append(
                        (node, {"function": call_name}, None)
                    )
                removal_parent_node = self._find_expression_statement_removal_parent_node(node)
                return self._add_to_removed_nodes(removal_parent_node)

    def visit_call_expression(self, node):
        """Identifies and handles method/function calls that use removed functions or variables."""
        # call_expression uses variable from removed declaration in argument list
        for child in node.children:
            if child.type == "argument_list":
                self._handle_call_expression_argument_list(child, node, None)
            # call_expression uses removed function
            if child.type == "identifier":
                self._handle_call_expression_identifier(child, node, None)


    def _handle_expression_statement_identifier(self, child, node, node_type):
        child_name = child.text.decode("utf-8")
        if child_name not in self.removed_nodes_with_types:
            self.removed_nodes_with_types[child_name] = node_type
        if (
            child_name in self.removed_declarations
            and node not in self.removed_nodes
        ):
            if self.mode == "replacement":
                return self.replaced_assignment_declarations.append(
                    (node, self.removed_nodes_with_types[child_name], ";")
                )
            else:
                return self._add_to_removed_nodes(node)
        for removal_node in self.functions_to_remove:
            if (
                removal_node.name == child_name
                and node not in self.removed_nodes
            ):
                if self.mode == "replacement":
                    return self.replaced_assignment_declarations.append(
                        (node, self.removed_nodes_with_types[child_name], ";")
                    )
                else:
                    return self._add_to_removed_nodes(node)

    def visit_expression_statement(self, node):
        """Identifies expression statements that use removed declarations and marks them for removal or replacement."""
        for child in node.children:
            if child.type in ["call_expression", "assignment_expression", "update_expression"]:
                for child_child in child.children:
                    if child_child.type == "identifier":
                        self._handle_expression_statement_identifier(child_child, node, None)
                    elif child_child.type in ["field_expression", "subscript_expression"]:
                        for child_child_child in child_child.children:
                            if child_child_child.type == "identifier":
                                self._handle_expression_statement_identifier(child_child_child, node, None)

    def visit_goto_statement(self, node):
        """Collects goto statements for later analysis of associated labels."""
        if node not in self.goto_statements:
            self.goto_statements.append(node)

    def _handle_go_to_labeled_statement(self, node):
        if node.parent is None:
            return self._add_to_removed_nodes(node)
        if len(node.parent.children) <= 3:
            self._add_to_removed_nodes(node.parent)
        else:
            self._add_to_removed_nodes(node)

    def visit_labeled_statement(self, node):
        """Handles labeled statements and removes associated goto statements if labels are removed."""
        for removal_node in self.removed_nodes:
            if not self._has_parent_node(node, removal_node):
                continue
            for child in node.children:
                if child.type != "statement_identifier":
                    continue
                child_name = child.text.decode("utf-8")
                if child_name in self.removed_nodes:
                    continue
                for goto_statement in self.goto_statements:
                    for goto_child in goto_statement.children:
                        goto_child_name = goto_child.text.decode("utf-8")
                        if goto_child.type == "statement_identifier":
                            if child_name == goto_child_name:
                                self._handle_go_to_labeled_statement(
                                    goto_statement
                                )
                return

    def _add_declaration_to_removal_nodes(self, node, child_name, node_type):
        for removal_node in self.global_variables_to_remove:
            if removal_node.name == child_name:
                self._add_to_removed_nodes(node)
                self._add_to_removed_declarations(child_name)
                if child_name not in self.removed_nodes_with_types:
                    self.removed_nodes_with_types[child_name] = node_type
                removal_parent_node = self._find_specific_parent_node(node, "if_statement")
                if not removal_parent_node:
                    removal_parent_node = self._find_specific_parent_node(node, "for_statement")
                self._add_to_removed_nodes(removal_parent_node)
                return

    def _handle_declaration_init_declarator(self, child, node, node_type):
        for i, child_child in enumerate(child.children):
            if child_child.type == "=":
                previous_node_child = child_child
            if child_child.type == "identifier":
                child_name = child_child.text.decode("utf-8")
                if i < 2:
                    self._add_declaration_to_removal_nodes(node, child_name, node_type)
                elif self.mode == "replacement" and child_name in self.removed_declarations:
                    self.replaced_assignment_declarations.append(
                        (node, node_type, previous_node_child)
                    )
            if child_child.type == "array_declarator":
                return self._handle_declaration_init_declarator(child_child, node, node_type)

    def _handle_declaration_array_declarator(self, child, node, node_type):
        for child_child in child.children:
            if child_child.type == "identifier":
                child_name = child_child.text.decode("utf-8")
                self._add_declaration_to_removal_nodes(node, child_name, node_type)

    def _handle_declaration_function_declarator(self, child, node, node_type):
        for child_child in child.children:
            if child_child.type == "identifier":
                child_name = child_child.text.decode("utf-8")
                for removal_node in self.functions_to_remove:
                    if removal_node.name == child_name:
                        self._add_to_removed_nodes(node)

    def _handle_declaration_identifier(self, child, node, node_type):
        child_name = child.text.decode("utf-8")
        self._add_declaration_to_removal_nodes(node, child_name, node_type)

    def visit_declaration(self, node):
        """Processes variable and function declarations, marking them for removal if they match."""
        node_type = None
        for child in node.children:
            if not node_type:
                node_type = self._get_node_type(child)
            if child.type == "identifier":
                return self._handle_declaration_identifier(child, node, node_type)
            elif child.type == "init_declarator":
                return self._handle_declaration_init_declarator(child, node, node_type)
            elif child.type == "array_declarator":
                return self._handle_declaration_array_declarator(child, node, node_type)
            elif child.type == "function_declarator":
                return self._handle_declaration_function_declarator(child, node, node_type)

    def visit_if_statement(self, node):
        """Marks if statements for removal if they use removed variables or are in removal list."""
        for removal_node in self.if_statements_to_remove:
            _, line_num = removal_node.name.split("_")
            if str(node.start_point[0]) == line_num:
                return self._add_to_removed_nodes(node)
        for child in node.children:
            if child.type == "parenthesized_expression":
                for child_child in child.children:
                    if child_child.type == "identifier":
                        if child_child.text.decode("utf-8") in self.removed_declarations:
                            return self._add_to_removed_nodes(node)

    def visit_for_statement(self, node):
        """Marks for loops for removal if they use removed variables or are in removal list."""
        for removal_node in self.for_statements_to_remove:
            _, line_num = removal_node.name.split("_")
            if str(node.start_point[0]) == line_num:
                return self._add_to_removed_nodes(node)
        for child in node.children:
            if child.type in ["call_expression", "assignment_expression", "update_expression"]:
                for child_child in child.children:
                    if child_child.type == "identifier":
                        if child_child.text.decode("utf-8") in self.removed_declarations:
                            self._add_to_removed_nodes(node)
                        return

    def visit_return_statement(self, node):
        """Handles return statements in replacement mode when they contain removed variables."""
        for i, child in enumerate(node.children):
            if child.type == "return":
                previous_node_child = child
            if child.type == "identifier":
                child_name = child.text.decode("utf-8")
                if child_name in self.removed_declarations:
                    self.replaced_assignment_declarations.append(
                        (node, self.removed_nodes_with_types[child_name], previous_node_child)
                    )

    def visit_identifier(self, node):
        """Identifies and handles identifier uses of removed declarations in different contexts."""
        node_text = node.text.decode("utf-8")
        if node_text in self.removed_declarations:
            parent_node = self._find_specific_parent_node(node, "if_statement")
            if parent_node is not None:
                if self._find_specific_parent_node(node, "compound_statement") is None:
                    return self._add_to_removed_nodes(parent_node)
            parent_node = self._find_specific_parent_node(node, "for_statement")
            if parent_node is not None:
                if self._find_specific_parent_node(node, "compound_statement") is None:
                    return self._add_to_removed_nodes(parent_node)
        if self.mode == "removal":
            return

        if node_text in self.removed_declarations:
            if node_text in self.removed_nodes_with_types:
                for replaced_node, _, _ in self.replaced_assignment_declarations:
                    if self._has_parent_node(node, replaced_node):
                        return
                self.replaced_assignment_declarations.append(
                    (node, self.removed_nodes_with_types[node.text.decode("utf-8")], None)
                )

    def _handle_struct_declaration(self, child, node, node_type):
        if node_type == "parameter_declaration":
            return
        declaration_removal_types = [
            "init_declarator", "pointer_declarator", "array_declarator"
        ]
        for child in node.children:
            if child.type == "identifier":
                self._add_to_removed_declarations(child.text.decode("utf-8"))
                if self.mode == "replacement":
                    return
                else:
                    return self._add_to_removed_nodes(node)
            if child.type in declaration_removal_types:
                for child_child in child.children:
                    if child_child.type == "identifier":
                        self._add_to_removed_declarations(child_child.text.decode("utf-8"))
                        if self.mode != "replacement":
                            return self._add_to_removed_nodes(node)
                    if child_child.type == "array_declarator":
                        for child_child_child in child_child.children:
                            if child_child_child.type == "identifier":
                                self._add_to_removed_declarations(child_child_child.text.decode("utf-8"))
                                if self.mode != "replacement":
                                    return self._add_to_removed_nodes(node)
                                break
                    if child_child.type == "=":
                        previous_node_child = child_child
                        return self.replaced_assignment_declarations.append(
                            (node, "struct", previous_node_child)
                        )

    def visit_struct_specifier(self, node):
        """Processes struct specifiers and removes struct declarations or their fields as needed."""
        for child in node.children:
            if child.type == "type_identifier":
                struct_name = child.text.decode("utf-8")
                removal_struct = False
                for removal_node in self.structs_to_remove:
                    if removal_node.name == struct_name:
                        if len(node.children) < 3:
                            node_type = node.parent.type
                            return self._handle_struct_declaration(node, node.parent, node_type)
                        removal_struct = True
                if not removal_struct:
                    return
            if child.type == "field_declaration_list":
                for struct_fields in child.children:
                    for struct_field in struct_fields.children:
                        if struct_field.type not in ["{", "}"]:
                            self._add_to_removed_nodes(struct_field)

    def replace_assignment_declarations(self, edits):
        for node, node_type, previous_node_child in self.replaced_assignment_declarations:
            if isinstance(node_type, dict):
                node_type = self.removed_nodes_with_types[node_type["function"]]
            overlapping_removal = False
            for removal_node in self.removed_nodes:
                if self._has_parent_node(node, removal_node):
                    overlapping_removal = True
                    break

            if overlapping_removal:
                continue
            if previous_node_child:
                constant_value = f" {self.constant_values[node_type]};".encode("utf-8")
                if previous_node_child == ";":
                    new_end_byte = node.start_byte
                    new_end_byte_with_constant = node.start_byte + len(constant_value)
                    new_end_point = node.start_point
                    new_end_point_with_constant = (node.start_point[0],
                                                node.start_point[1]
                                                + len(constant_value.decode("utf-8")))
                else:
                    new_end_byte = previous_node_child.end_byte
                    new_end_byte_with_constant = previous_node_child.end_byte + len(constant_value)
                    new_end_point = previous_node_child.end_point
                    new_end_point_with_constant = (node.end_point[0],
                                                previous_node_child.end_point[1]
                                                + len(constant_value.decode("utf-8")))
            else:
                # When the replaced declaration is an identifier
                constant_value = f"{self.constant_values[node_type]}".encode("utf-8")
                new_end_byte = node.start_byte
                new_end_byte_with_constant = node.start_byte + len(constant_value)
                new_end_point = node.start_point
                new_end_point_with_constant = (node.start_point[0],
                                            node.start_point[1]
                                            + len(constant_value.decode("utf-8")))
            edits.append({
                "start_byte": node.start_byte,
                "old_end_byte": node.end_byte,
                "new_end_byte": new_end_byte,
                "new_end_byte_with_constant": new_end_byte_with_constant,
                "start_point": node.start_point,
                "old_end_point": node.end_point,
                "new_end_point": new_end_point,
                "new_end_point_with_constant": new_end_point_with_constant,
                "new_text": constant_value
            })


    def remove_nodes(self, nodes_to_remove: set, mode: str) -> str:
        """
        Main entry point for node removal with support for three modes.
        
        Args:
            nodes_to_remove: Set of nodes to be removed
            mode: Strategy for handling nodes - 'removal', 'replacement', or 'combination'
                - 'removal': Simply removes the identified nodes
                - 'replacement': Replaces nodes with constant values based on their type
                - 'combination': Iterates between replacement and removal until fixed point
        
        Returns:
            Modified source code as a string with nodes removed or replaced
        """
        if mode not in ["removal", "replacement", "combination"]:
            raise ValueError(
                f"Unknown mode: {mode}. Must be 'removal', 'replacement', or "
                f"'combination'."
            )
        
        sorted_nodes: list[Any] = sorted([node for node in nodes_to_remove],
                                          key=lambda x: x.name)
        self.mode = mode
        parser = parsers.get_parser(self.LANGUAGE)
        tree = parser.parse(self.content.encode("utf-8"))

        self.functions_to_remove: list[Any] = [node for node in sorted_nodes if node.node_type == "function"]
        self.global_variables_to_remove: list[Any] = [node for node in sorted_nodes if node.node_type == "global_variable"]
        self.structs_to_remove: list[Any] = [node for node in sorted_nodes if node.node_type == "struct"]
        self.if_statements_to_remove: list[Any] = [node for node in sorted_nodes if node.node_type == "if_statement"]
        self.for_statements_to_remove: list[Any] = [node for node in sorted_nodes if node.node_type == "for_statement"]

        try:
            self.traverse_node(tree.root_node)
        except Exception as e:
            print("Modification exception")
            print(traceback.format_exc())
            raise e
        self.removed_nodes.sort(key=lambda node: node.end_byte, reverse=True)

        edits = []
        modified_code = tree.root_node.text
        visited_nodes = [False] * len(self.removed_nodes)
        for i, removed_node in enumerate(self.removed_nodes):
            if visited_nodes[i]:
                continue
            visited_nodes[i] = True

            overlapping_nodes = [removed_node]

            for j in range(i + 1, len(self.removed_nodes)):
                other_node = self.removed_nodes[j]
                if (other_node.start_byte <= removed_node.end_byte and
                    other_node.end_byte >= removed_node.start_byte):
                    visited_nodes[j] = True
                    overlapping_nodes.append(other_node)
                elif other_node.start_byte > removed_node.end_byte:
                    break

            start_byte = min(node.start_byte for node in overlapping_nodes)
            end_byte = max(node.end_byte for node in overlapping_nodes)
            start_point = min(node.start_point for node in overlapping_nodes)
            end_point = max(node.end_point for node in overlapping_nodes)
            edits.append({
                "start_byte": start_byte,
                "old_end_byte": end_byte,
                "new_end_byte": start_byte,
                "start_point": start_point,
                "old_end_point": end_point,
                "new_end_point": start_point,
            })
        if mode in ["replacement", "combination"]:
            self.replace_assignment_declarations(edits)
            edits.sort(key=lambda edit: edit["start_byte"], reverse=True)
        for edit in edits:
            # Apply the edit to the tree
            if "new_text" in edit and mode in ["replacement", "combination"]:
                tree.edit(
                    start_byte=edit["start_byte"],
                    old_end_byte=edit["old_end_byte"],
                    new_end_byte=edit["new_end_byte_with_constant"],
                    start_point=edit["start_point"],
                    old_end_point=edit["old_end_point"],
                    new_end_point=edit["new_end_point_with_constant"],
                )
                # Update the source code
                modified_code = (
                    modified_code[: edit["start_byte"]] +
                    modified_code[edit["start_byte"]:edit["new_end_byte"]] +
                    edit["new_text"] +
                    modified_code[edit["old_end_byte"]:]
                )
            else:
                tree.edit(
                    start_byte=edit["start_byte"],
                    old_end_byte=edit["old_end_byte"],
                    new_end_byte=edit["new_end_byte"],
                    start_point=edit["start_point"],
                    old_end_point=edit["old_end_point"],
                    new_end_point=edit["new_end_point"],
                )
                # Update the source code
                modified_code = (
                    modified_code[: edit["start_byte"]] +
                    modified_code[edit["start_byte"]:edit["new_end_byte"]] +
                    modified_code[edit["old_end_byte"]:]
                )

        parser = parsers.get_parser(self.LANGUAGE)
        updated_tree = parser.parse(modified_code, tree)
        return remove_empty_lines(updated_tree.root_node.text.decode("utf-8"))


class JavaDeclarationRemoval(ASTRemoval):
    LANGUAGE = "java"
    count = 0

    def __init__(self, content, graph):
        super().__init__(content, graph)
        self.removed_nodes = []
        self.parser = parsers.get_parser(self.LANGUAGE)
        self.tree = self.parser.parse(content.encode("utf-8"))
        self.constant_values = {
            "int": "42",
            "boolean": "true",
            "char": "'a'",
            "void": "",
            "Boolean": "true",
            "Integer": "42",
            "String": "\"\"",
            "Object": "null",
            "double": "0.0",
            "float": "0.0f",
            "Double": "0.0",
            "Float": "0.0f",
            "byte": "0",
            "Byte": "0",
            "short": "0",
            "Short": "0",
            "long": "0L",
            "Long": "0L",

        }

    def visit_default(self, node):
        pass

    def exit_default(self, node):
        pass

    def update_tree_incrementally(self, new_content):
        old_tree = self.tree
        self.content = new_content
        self.tree = self.parser.parse(
            new_content.encode("utf-8"),
            old_tree=old_tree
        )

    def delete_nodes(self, tree=None):
        if tree is None:
            tree = self.tree
        self.removed_nodes = self.filter_enclosing_nodes(self.removed_nodes)  # remove duplicates and nested nodes
        self.removed_nodes.sort(key=lambda node: node.start_byte, reverse=True)
        source_code = tree.root_node.text
        modified_code = bytearray(source_code)

        for node in self.removed_nodes:
            start = node.start_byte
            end = node.end_byte
            del modified_code[start:end]

        modified_content = modified_code.decode("utf-8")

        return modified_content

    def get_node_visitor(self, node):
        visitors = {
            "method_declaration": self.visit_function_definition,
            "call_expression": self.visit_call_expression,
        }
        return visitors.get(node.type, self.visit_default)

    def get_node_exit(self, node):
        exit_funcs = {
        }
        return exit_funcs.get(node.type, self.exit_default)

    def is_contained(self, inner, outer):
        return (outer.start_byte <= inner.start_byte and outer.end_byte >= inner.end_byte)

    def filter_enclosing_nodes(self, nodes):
        result = []
        for node in nodes:
            if not any(self.is_contained(node, other) and node != other for other in nodes):
                result.append(node)
        return result

    def break_inheritance(self, nodes_to_remove: set):
        """Removes inheritance declarations (superclass and super_interfaces) from classes being removed."""
        parser = parsers.get_parser(self.LANGUAGE)
        tree = parser.parse(self.content.encode("utf-8"))
        for node in nodes_to_remove:

            class_name = node.name

            query = parsers.JAVA_LANGUAGE.query(f"""
            (class_declaration
            name: (identifier) @class_name
            (#eq? @class_name "{class_name}")) @class
            """)

            captures = query.captures(tree.root_node)
            class_node = None
            for node, capture_name in captures:
                if capture_name == "class":
                    class_node = node
                    break

            if class_node is None:
                continue

            for child in class_node.children:
                if child.type == "superclass" or child.type == "super_interfaces":
                    self.removed_nodes.append(child)

            for child in class_node.children:
                if child.type != "class_body":
                    continue
                for member in child.children:
                    if member.type != "constructor_declaration":
                        continue
                    for ctor_child in member.children:
                        if ctor_child.type != "constructor_body":
                            continue
                        for statement in ctor_child.children:
                            if statement.type == "explicit_constructor_invocation":
                                ctor_field = statement.child_by_field_name("constructor")
                                if ctor_field and ctor_field.type == "super":
                                    self.removed_nodes.append(statement)

        result = self.delete_nodes(tree)

        return result

    def visit_super_calls(self, tree):
        """Handles super() constructor calls and removes their associated superclass inheritance."""
        query = parsers.JAVA_LANGUAGE.query("""
        (
        (explicit_constructor_invocation
            constructor: (super)
            arguments: (argument_list) @args) @super_ctor
        )
        """)
        capt = query.captures(tree.root_node)

        for node, _ in capt:
            current = node
            while current is not None and current.type != "class_declaration":
                current = current.parent
            if current is None:
                # \No class declaration found for super call
                continue
            for child in current.children:
                if child.type == "superclass" or child.type == "super_interfaces":
                    self.removed_nodes.append(child)
                    break

            self.removed_nodes.append(node)

    def visit_function_definition(self, node):
        """Collects method definition nodes to be removed."""
        function_name = None
        for n in node.children:
            if n.type == "identifier":
                function_name = n.text.decode("utf-8")
                break
        if any((node.name == function_name and node.node_type == "function")
               for node in self.nodes_to_remove):
            self.removed_nodes.append(node)

    def visit_call_expression(self, node):
        """Identifies and handles method calls that should be removed or replaced."""
        child = node.children[0]
        assert child.type == "expression"
        match child.children[0].type:
            case "member_expression":
                call_name = child.children[0].children[-1].text.decode("utf-8")
            case "identifier":
                call_name = child.children[0].text.decode("utf-8")
            case _:
                raise Exception("Unknown node")
        if any(node.name == call_name for node in self.nodes_to_remove):
            self.removed_nodes.append(node)
            current_node = node
            while True:
                match current_node.type:
                    case "assignment_expression":
                        self.removed_nodes.remove(node)
                        self.removed_nodes.append(current_node)
                        break
                    case "function_body":
                        break
                    case None:
                        break
                    case _:
                        current_node = current_node.parent

    def remove_nodes(self, nodes_to_remove: set, mode: str = "removal") -> str:
        """
        Main entry point for node removal with support for three modes.
        
        Args:
            nodes_to_remove: Set of nodes to be removed
            mode: Strategy for handling nodes - 'removal' or 'replacement'
                - 'removal': Simply removes the identified nodes
                - 'replacement': Replaces nodes with constant values based on their type
                - 'combination': Iterates between replacement and removal until fixed point
        
        Returns:
            Modified source code as a string
        """
        if mode not in ["removal", "replacement"]:
            raise ValueError(
                f"Unknown mode: {mode}. Must be 'removal' or 'replacement'"
            )
    
        self.mode = mode
        
        if mode == "replacement":
            result = self.replace_nodes(nodes_to_remove)
        else:
            result = self.remove_nodes_(nodes_to_remove)

        return result

    def remove_local_variable(self, node_to_remove, tree):
        """Removes local variable declarations and all statements that use them."""
        name = node_to_remove.name
        decl_q = parsers.JAVA_LANGUAGE.query(f"""
        (local_variable_declaration
          declarator: (variable_declarator
            name: (identifier) @var_name 
            (#eq? @var_name "{name}")
          )
        ) @decl
        """)
        for n, cap in decl_q.captures(tree.root_node):
            if cap == "decl":
                self.removed_nodes.append(n)

        id_q = parsers.JAVA_LANGUAGE.query(f"""
        (identifier) @id (#eq? @id "{name}")
        """)
        for n, cap in id_q.captures(tree.root_node):
            if cap == "id":
                stmt = self.find_ancestor(n, 'statement')
                if stmt:
                    self.removed_nodes.append(stmt)

    def remove_class(self, node_to_remove, tree):
        """Removes a class or interface declaration and all usages of that type."""
        name = node_to_remove.name

        query = parsers.JAVA_LANGUAGE.query(f"""
        (class_declaration
            name: (identifier) @class_name
            (#eq? @class_name "{name}")) @class_node

        """)
        query2 = parsers.JAVA_LANGUAGE.query(f"""
                                      (interface_declaration
        name: (identifier) @interface_name
        (#eq? @interface_name "{name}")) @interface_node
         """)
        class_node = None
        for node, capture in query.captures(tree.root_node):
            if capture == "class_node":
                class_node = node
                break

        if class_node is None:
            for node, capture in query2.captures(tree.root_node):
                if capture == "interface_node":
                    class_node = node
                    break
        if class_node is None:
            return

        self.removed_nodes.append(class_node)

        usage_query = parsers.JAVA_LANGUAGE.query(f"""
        (object_creation_expression type: (type_identifier) @used_type
        (#eq? @used_type "{name}")) @expr

        (cast_expression type: (type_identifier) @used_type
        (#eq? @used_type "{name}")) @expr

        (local_variable_declaration
            type: (type_identifier) @used_type
            (#eq? @used_type "{name}")) @stmt

        (field_declaration
            type: (type_identifier) @used_type
            (#eq? @used_type "{name}")) @stmt
        """)
        for node, cap in usage_query.captures(tree.root_node):
            if cap in {"expr", "stmt"}:
                self.removed_nodes.append(node)

    def remove_function(self, node_to_remove, tree):
        """Removes a method definition and all invocations of that method."""
        name = node_to_remove.name
        method_query_str = f'''(method_declaration name: (identifier) @func_name (#eq? @func_name "{name}")) @method'''

        call_query_str = f'''(method_invocation name:(identifier) @call_name (#eq? @call_name "{name}")) @call'''

        method_query = parsers.JAVA_LANGUAGE.query(method_query_str)
        call_query = parsers.JAVA_LANGUAGE.query(call_query_str)

        for node, capture_name in method_query.captures(tree.root_node):
            if capture_name == "method":
                function_args = ""
                for child in node.children:
                    if child.type == "formal_parameters":
                        function_args = child.text.decode("utf-8")
                        break
                if function_args == node_to_remove.args:
                    self.removed_nodes.append(node)
        for node, capture_name in call_query.captures(tree.root_node):
            if capture_name == "call":
                current_node = node
                while current_node.parent is not None:
                    if current_node.type in {
                        "local_variable_declaration",
                        "assignment_expression",
                        "expression_statement",
                        "return_statement",
                        "field_declaration",

                    }:
                        self.removed_nodes.append(current_node)
                        break
                    current_node = current_node.parent
        for node, capture_name in call_query.captures(tree.root_node):
            if capture_name == "call":
                self.removed_nodes.append(node)

    def remove_constructor(self, node_to_remove, tree):
        """Removes a constructor declaration."""
        name = node_to_remove.name
        constructor_query_str = f'''(constructor_declaration name: (identifier) @ctor_name (#eq? @ctor_name "{name}")) @ctor'''
        constructor_query = parsers.JAVA_LANGUAGE.query(constructor_query_str)
        for node, capture_name in constructor_query.captures(tree.root_node):
            if capture_name == "ctor":
                self.removed_nodes.append(node)

    def remove_field(self, node_to_remove, tree):
        """Removes a field declaration and all statements that access it."""
        name = node_to_remove.name
        field_decl_query_str = f'''( (field_declaration declarator: (variable_declarator name: (identifier) @field_name value: (_) @field_value ) ) (#eq? @field_name "{name}") )'''

        field_access_query_str = f'''
        (expression_statement
            (assignment_expression
            left: (field_access
                    field: (identifier) @field_name
                    (#eq? @field_name "{name}")))) @stmt

        (expression_statement
            (field_access
            field: (identifier) @field_name
            (#eq? @field_name "{name}"))) @stmt

        (return_statement
            (field_access
            field: (identifier) @field_name
            (#eq? @field_name "{name}"))) @stmt'''

        access_query = parsers.JAVA_LANGUAGE.query(field_access_query_str)

        field_query = parsers.JAVA_LANGUAGE.query(field_decl_query_str)

        for node, capture_name in field_query.captures(tree.root_node):
            if capture_name == "field_value":
                self.removed_nodes.append(node)
                if node.prev_sibling is not None:
                    if node.prev_sibling.type == "=":
                        self.removed_nodes.append(node.prev_sibling)

        for node, capture_name in access_query.captures(tree.root_node):
            if capture_name == "stmt":
                self.removed_nodes.append(node)
        decl_query_str = f'''
            (field_declaration
            declarator: (variable_declarator
                name: (identifier) @field_name
                (#eq? @field_name "{name}")
            )
            ) @decl
            '''
        access_captures = access_query.captures(tree.root_node)
        decl_query = parsers.JAVA_LANGUAGE.query(decl_query_str)
        decl_captures = decl_query.captures(tree.root_node)

        if decl_captures and not access_captures:
            for node, capture_name in decl_captures:
                if capture_name == "decl":
                    self.removed_nodes.append(node)

    def remove_nodes_(self, nodes_to_remove: set):
        """Main removal logic that identifies and removes specific node types from the Java source."""
        self.removed_nodes = []
        self.count += 1
        parser = parsers.get_parser(self.LANGUAGE)
        tree = parser.parse(self.content.encode("utf-8"))
        self.nodes_to_remove = nodes_to_remove

        for node_to_remove in self.nodes_to_remove:
            node_type = node_to_remove.node_type
            match node_type:
                case "function":
                    self.remove_function(node_to_remove, tree)
                case "constructor":
                    self.remove_constructor(node_to_remove, tree)
                case "field":
                    self.remove_field(node_to_remove, tree)
                case "class":
                    self.remove_class(node_to_remove, tree)
                case "local_variable":
                    self.remove_local_variable(node_to_remove, tree)

        result = self.delete_nodes(tree)

        return result

    def find_ancestor(self, node, typ):
        cur = node.parent
        while cur:
            if cur.type == typ:
                return cur
            cur = cur.parent
        return None

    def node_in_subtree(self, target, root):
        if root is None:
            return False
        if target == root:
            return True
        for c in root.children:
            if self.node_in_subtree(target, c):
                return True
        return False

    def is_read_context(self, node):
        decl = self.find_ancestor(node, 'variable_declarator')
        if decl and decl.child_by_field_name('name') == node:
            return False

        param = self.find_ancestor(node, 'formal_parameter')
        if param and param.child_by_field_name('name') == node:
            return False

        assign = self.find_ancestor(node, 'assignment_expression')
        if assign:
            left = assign.child_by_field_name('left') or (assign.children[0] if assign.children else None)
            if left and self.node_in_subtree(node, left):
                return False

        return True

    def replace_field(self, node_to_replace, tree):
        """Finds field access nodes to replace with constant values in replacement mode."""
        name = node_to_replace.name
        access_query = parsers.JAVA_LANGUAGE.query(f'''
        ;; Initializers: int z = x; int y = this.x;
        (variable_declarator
            value: (identifier) @use
            (#eq? @use {name}))
        (variable_declarator
            value: (field_access
                    field: (identifier) @field_name
                    (#eq? @field_name {name})) @use)

        ;; RHS of assignments: z = x; z = this.x;
        (assignment_expression
            right: (identifier) @use
            (#eq? @use {name}))
        (assignment_expression
            right: (field_access
                    field: (identifier) @field_name
                    (#eq? @field_name {name})) @use)

        ;; Argument: approve(x); approve(this.x);
        (argument_list
            (identifier) @use
            (#eq? @use {name}))
        (argument_list
            (field_access
                    field: (identifier) @field_name
                    (#eq? @field_name {name})) @use)

        ;; Return: return x; return this.x;
        (return_statement
            (identifier) @use
            (#eq? @use {name}))
        (return_statement
            (field_access
                    field: (identifier) @field_name
                    (#eq? @field_name {name})) @use)

        ;; Binary expression: x + y, y + x, this.x + ... 
        (binary_expression
            left: (identifier) @use
            (#eq? @use {name}))
        (binary_expression
            right: (identifier) @use
            (#eq? @use {name}))
        (binary_expression
            left: (field_access
                    field: (identifier) @field_name
                    (#eq? @field_name {name})) @use)
        (binary_expression
            right: (field_access
                    field: (identifier) @field_name
                    (#eq? @field_name {name})) @use)
        ''')

        decl_query_str = f'''
            (field_declaration
            type: (_) @field_type
            declarator: (variable_declarator
                name: (identifier) @field_name
                (#eq? @field_name "{name}")
            )
            ) @decl
            '''

        query = parsers.JAVA_LANGUAGE.query(decl_query_str)
        captures = query.captures(tree.root_node)

        field_type = None
        source_code = tree.root_node.text
        code = bytearray(source_code)

        for node, cap in captures:
            text = code[node.start_byte:node.end_byte].decode()
            if cap == "field_name":
                field_name = text
            elif cap == "field_type":
                field_type = text

        query = access_query
        captures = query.captures(tree.root_node)  # capture access
        nodes_to_replace = []
        for node, cap_name in captures:
            if not self.is_read_context(node):  # make sure it's not LHS
                continue
            nodes_to_replace.append(node)

        result = (nodes_to_replace, field_type)

        return result

    def replace_function(self, node_to_replace, tree):
        """Finds method invocations to replace with constant return values in replacement mode."""
        name = node_to_replace.name
        source_code = tree.root_node.text

        decl_query_str = f'''
            (method_declaration type: (_) @return_type 
            name: (identifier) @func_name 
            parameters: (formal_parameters) @params 
            (#eq? @func_name {name} ))'''

        decl_query = parsers.JAVA_LANGUAGE.query(decl_query_str)
        rt = None
        for node, capture_name in decl_query.captures(tree.root_node):
            if capture_name == "return_type":
                rt = source_code[node.start_byte:node.end_byte].decode("utf-8").strip()

        call_query_str = f'''
        (
        (method_invocation
            name: (identifier) @call_name (#eq? @call_name "{name}")
            arguments: (argument_list) @args
        ) @call
        )
        '''

        call_query = parsers.JAVA_LANGUAGE.query(call_query_str)

        nodes_to_replace = []
        call_arg_types = {}
        for node, capture_name in call_query.captures(tree.root_node):
            if capture_name == "call":
                nodes_to_replace.append(node)
                arg_list_node = None
                for child in node.children:
                    if child.type == "argument_list":
                        arg_list_node = child
                        break
                if arg_list_node:
                    arg_types = self.extract_arg_types(
                        arg_list_node)  # tried handling overloading here, if failed, just boils down to return type
                    arg_types = arg_types if arg_types else rt
                else:
                    arg_types = rt
                call_arg_types[node] = arg_types

        result = (nodes_to_replace, rt)

        return result

    def replace_local_variable(self, node_to_replace, tree):
        """Finds read uses of local variables and returns them with their type for replacement mode."""
        name = node_to_replace.name
        decl_query = parsers.JAVA_LANGUAGE.query(f'''
        (local_variable_declaration
            type: (_) @var_type
            declarator: (variable_declarator
                name: (identifier) @var_name (#eq? @var_name "{name}")
            )
        )
        ''')
        var_type = None
        for n, cap in decl_query.captures(tree.root_node):
            if cap == "var_type":
                var_type = tree.root_node.text[n.start_byte:n.end_byte].decode("utf-8").strip()

        use_query = parsers.JAVA_LANGUAGE.query(f'''
        ;; initializer RHS: int x = a;
        (variable_declarator
            name: (_)
            value: (identifier) @use1 (#eq? @use1 "{name}")
        )
        ;; assignment RHS: x = a;
        (assignment_expression right: (identifier) @use2 (#eq? @use2 "{name}"))
        ;; return x;
        (return_statement (identifier) @use3 (#eq? @use3 "{name}"))
        ;; argument: foo(x)
        (argument_list (identifier) @use4 (#eq? @use4 "{name}"))
        ;; binary ops: x  y, y  x
        (binary_expression left: (identifier) @use5 (#eq? @use5 "{name}"))
        (binary_expression right: (identifier) @use6 (#eq? @use6 "{name}"))
        ''')

        to_replace = []
        for n, cap in use_query.captures(tree.root_node):
            if cap.startswith("use") and self.is_read_context(n):
                to_replace.append(n)

        result = to_replace, var_type

        return result

    def replace_nodes(self, nodes_to_remove: set):
        """Replaces nodes with appropriate constant values based on their type in replacement mode."""
        parser = parsers.get_parser(self.LANGUAGE)
        tree = parser.parse(self.content.encode("utf-8"))
        source_code = tree.root_node.text
        self.nodes_to_remove = nodes_to_remove
        nodes_to_replace = []
        mapping = dict()
        for node in self.nodes_to_remove:
            if node.node_type == 'field':
                fields_to_replace = self.replace_field(node, tree)
                nodes_to_replace.append(fields_to_replace)

            elif node.node_type == 'function':
                functions_to_replace = self.replace_function(node, tree)
                nodes_to_replace.append(functions_to_replace)
            elif node.node_type == 'local_variable':
                locals_to_replace = self.replace_local_variable(node, tree)
                nodes_to_replace.append(locals_to_replace)
        for arr, typ in nodes_to_replace:
            for n in arr:
                mapping[n] = typ
        nodes_to_replace = [n for n, _ in nodes_to_replace]
        nodes_to_replace = [item for sub in nodes_to_replace for item in sub]

        nodes_to_replace = sorted(set(nodes_to_replace), key=lambda n: n.start_byte, reverse=True)
        nodes_to_replace = self.filter_enclosing_nodes(nodes_to_replace)
        modified_code = bytearray(source_code)

        for node in nodes_to_replace:

            constant_value = self.constant_values.get(
                mapping.get(node, None), None
            )
            if constant_value is None:
                typ = mapping.get(node, None)
                if typ:
                    constant_value = f"({typ}) null"
            replacement_text = constant_value.encode("utf-8")
            start = node.start_byte
            end = node.end_byte
            modified_code[start:end] = replacement_text
        modified_code = modified_code.decode("utf-8")  # type: ignore[assignment]
        self.content = modified_code  # type: ignore[assignment]

        result = modified_code

        return result

    def extract_param_types(self, params_text):
        params_text = params_text.strip()
        if params_text.startswith("(") and params_text.endswith(")"):
            inner = params_text[1:-1].strip()
        else:
            inner = params_text
        if not inner:
            return tuple()
        params = [p.strip() for p in inner.split(",")]
        types = []
        for p in params:
            tokens = p.split()
            if tokens:
                types.append(tokens[0])
        return tuple(types)

    def get_literal_type(self, node):

        typemap = {
            "decimal_integer_literal": "int",
            "boolean_literal": "boolean",
            "character_literal": "char"
        }
        return typemap.get(node.type, None)

    def extract_arg_types(self, arg_list_node):

        arg_types = []
        for child in arg_list_node.children:
            t = self.get_literal_type(child)
            if t is not None:
                arg_types.append(t)
        return tuple(arg_types)


AST_REMOVALS = {
    "solidity": SolidityDeclarationRemoval,
    "c": CDeclarationRemoval,
    "java": JavaDeclarationRemoval,
}
