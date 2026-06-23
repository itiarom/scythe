import re

from scythe import parsers
from scythe.rewrites.base import ASTRemoval, remove_empty_lines


class SolidityDeclarationRemoval(ASTRemoval):
    LANGUAGE = "solidity"
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
            "expression_statement": self.visit_use_site_statement,
            "return_statement": self.visit_use_site_statement,
            "variable_declaration_statement": self.visit_use_site_statement,
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
        name = parsers.declaration_name(node)
        self.contract_scope.append(name)
        if name in self.removed_contracts:
            self._mark(node)

    def exit_contract_declaration(self, node):
        if self.contract_scope:
            self.contract_scope.pop()

    def visit_function_definition(self, node):
        name = parsers.declaration_name(node)
        signature = parsers.parameter_signature(node)
        if self._is_selected(name, "function", signature):
            self.removed_nodes.append(node)

    def visit_modifier_definition(self, node):
        if self._is_selected(parsers.declaration_name(node), "modifier"):
            self.removed_nodes.append(node)

    def visit_struct_definition(self, node):
        if self._is_selected(parsers.declaration_name(node), "struct"):
            self.removed_nodes.append(node)


    def visit_state_variable_declaration(self, node):
        if self._is_selected(parsers.declaration_name(node), "state_var"):
            self.removed_nodes.append(node)

    def visit_event_definition(self, node):
        if self._is_selected(parsers.declaration_name(node), "event"):
            self.removed_nodes.append(node)

    def _callee_name(self, node):
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
        current = node
        while current is not None:
            if current.type.endswith("_statement"):
                return current
            current = current.parent
        return None

    def visit_call_expression(self, node):
        call_name = self._callee_name(node)
        if call_name is None:
            return
        if call_name in self.removed_events or self._name_fully_removed(call_name):
            self._mark(self._enclosing_statement(node) or node)

    def visit_emit_statement(self, node):
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
        # A modifier_invocation is also how a derived constructor calls a base
        # constructor (`function D(...) Base(args)`), so removing a contract must
        # drop these too -- otherwise the call to the deleted base is left dangling
        # (undeclared identifier) and the contract can never be removed on its own.
        for child in node.children:
            if child.type == "identifier":
                name = child.text.decode("utf-8")
                if name in self.removed_modifiers or name in self.removed_contracts:
                    self._mark(node)
                return

    def visit_using_directive(self, node):
        for child in node.children:
            if child.type in ("type_alias", "user_defined_type", "identifier"):
                if child.text.decode("utf-8") in self.removed_contracts:
                    self._mark(node)
                return

    @staticmethod
    def _inheritance_base(node):
        return node.text.decode("utf-8").split("(")[0].strip()

    def visit_inheritance_specifier(self, node):
        if self._inheritance_base(node) not in self.removed_contracts:
            return
        parent = node.parent
        siblings = parent.children
        specs = [c for c in siblings if c.type == "inheritance_specifier"]
        surviving = [s for s in specs
                     if self._inheritance_base(s) not in self.removed_contracts]
        if not surviving:
            is_kw = next((c for c in siblings if c.type == "is"), None)
            start = is_kw.start_byte if is_kw else specs[0].start_byte
            end = max(s.end_byte for s in specs)
            self.removed_ranges.append((start, end))
        else:
            idx = siblings.index(node)
            start, end = node.start_byte, node.end_byte
            if idx > 0 and siblings[idx - 1].type == ",":
                start = siblings[idx - 1].start_byte
            elif idx + 1 < len(siblings) and siblings[idx + 1].type == ",":
                end = siblings[idx + 1].end_byte
            self.removed_ranges.append((start, end))

    def _references_removed_value(self, node):
        stack = list(node.children)
        while stack:
            n = stack.pop()
            if n.type in ("identifier", "type_name", "user_defined_type"):
                if n.text.decode("utf-8") in self.removed_value_refs:
                    return True
            stack.extend(n.children)
        return False

    def visit_use_site_statement(self, node):
        if self.removed_value_refs and self._references_removed_value(node):
            self._mark(node)

    def visit_use_site_control_flow(self, node):
        if not self.removed_value_refs:
            return
        body = node.child_by_field_name("body")
        body_id = body.id if body is not None else None
        if any(child.id != body_id and self._references_removed_value(child)
               for child in node.children):
            self._mark(node)

    def _expand_dead_locals(self, tree):
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

    @classmethod
    def build_candidate(cls, base_content, graph, removed, mode, table=None):
        # Solidity's edits are set-dependent (reference/emit/modifier-invocation
        # removal keyed on the whole removed set, the dead-local fixpoint, struct
        # placeholder synthesis), so unlike C/Java there is no per-node edit table
        # to precompute -- every candidate runs the full traversal. The one
        # reusable artifact is the parse, so memoize it across the pass; flatten/
        # break keep their own parse via the slow path.
        sel = set(removed)
        if mode == "removal":
            if table is None:
                table = parsers.get_parser(cls.LANGUAGE).parse(
                    base_content.encode("utf-8"))
            return cls(base_content, graph)._remove_with_tree(sel, table), table
        return cls._slow_candidate(base_content, graph, sel, mode), table

    def remove_nodes(self, nodes_to_remove: set, mode: str) -> str:
        if mode not in ["removal"]:
            raise ValueError(f"Unknown mode: {mode}. Must be 'removal'")
        tree = parsers.get_parser(self.LANGUAGE).parse(
            self.content.encode("utf-8"))
        return self._remove_with_tree(nodes_to_remove, tree)

    def _remove_with_tree(self, nodes_to_remove, tree):
        # Removal body, parameterized on an already-parsed tree so build_candidate
        # can reuse one parse across every candidate in a pass.
        self.nodes_to_remove = nodes_to_remove
        self.removed_nodes = []
        self.removed_ranges = []
        self.removed_contracts = {n.name for n in nodes_to_remove
                                  if n.node_type == "contract"}
        self.removed_events = {n.name for n in nodes_to_remove
                               if n.node_type == "event"}
        self.removed_modifiers = {n.name for n in nodes_to_remove
                                  if n.node_type == "modifier"}
        self.removed_value_refs = {n.name for n in nodes_to_remove
                                   if n.node_type in ("state_var", "var", "struct")}
        for n in self.graph.nodes:
            if getattr(n.parent, "name", None) in self.removed_contracts:
                if n.node_type == "modifier":
                    self.removed_modifiers.add(n.name)
                elif n.node_type == "event":
                    self.removed_events.add(n.name)
                elif n.node_type in ("state_var", "var", "struct"):
                    self.removed_value_refs.add(n.name)

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

        self._expand_dead_locals(tree)

        self.traverse_node(tree.root_node)

        ranges = [(n.start_byte, n.end_byte) for n in self.removed_nodes]
        ranges.extend(self.removed_ranges)
        ranges.sort()
        merged = []
        for start, end in ranges:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])

        edits = [(s, e, b"") for s, e in merged]
        edits += self._placeholder_retype_edits(tree, merged)
        if not edits:
            return self.content

        source = self.content.encode("utf-8")
        for start, end, repl in sorted(edits, key=lambda e: e[0], reverse=True):
            source = source[:start] + repl + source[end:]
        return remove_empty_lines(source.decode("utf-8"))


    def _enclosing_contract_node(self, node):
        cur = node.parent
        while cur is not None:
            if cur.type in ("contract_declaration", "interface_declaration"):
                return cur
            cur = cur.parent
        return None

    @staticmethod
    def _struct_members(struct_node):
        body = next((c for c in struct_node.children
                     if c.type == "struct_body"), None)
        if body is None:
            return []
        return [(parsers.declaration_name(m), m.text.decode("utf-8"))
                for m in body.children if m.type == "struct_member"]

    def _retype_text(self, text, names):
        for name in names:
            text = re.sub(rf"\b{re.escape(name)}\b",
                          self.PLACEHOLDER_STRUCT, text)
        return text

    def _placeholder_struct_decl(self, members, names):
        seen, fields = set(), []
        for fname, text in members:
            if fname in seen:
                continue
            seen.add(fname)
            fields.append(self._retype_text(text, names))
        return f"struct {self.PLACEHOLDER_STRUCT} {{ {' '.join(fields)} }}"

    def _placeholder_insert_pos(self, tree, contract_node):
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
        removed = {n.name for n in self.nodes_to_remove
                   if n.node_type == "struct"
                   and n.name != self.PLACEHOLDER_STRUCT}
        if not removed:
            return []

        members_by_struct = {}
        existing = {}
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
        seeds = {}
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
                continue
            if covered(n.start_byte, n.end_byte, existing_ranges):
                continue
            edits.append((n.start_byte, n.end_byte,
                          self.PLACEHOLDER_STRUCT.encode("utf-8")))
            cnode = self._enclosing_contract_node(n)
            key = id(cnode) if cnode is not None else None
            seeds.setdefault(key, (cnode, set()))[1].add(name)

        if not edits:
            return []

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


    @staticmethod
    def _contract_body(contract_node):
        return next((c for c in contract_node.children
                     if c.type == "contract_body"), None)

    @staticmethod
    def _is_constructor(member, contract_name):
        if member.type == "constructor_definition":
            return True
        return (member.type == "function_definition"
                and parsers.declaration_name(member) == contract_name)

    def _inheritance_clause_edit(self, child, base_name, replacement_names):
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
        extra = [n for n in replacement_names
                 if n not in {self._inheritance_base(s) for s in specs}]
        return (start, end, (", ".join(extra)) if extra else "")

    def _base_ctor_call_edits(self, child, base_name):
        # A derived contract calls its base constructor via `Base(args)` (a
        # modifier_invocation); when Base is flattened away that call must be
        # removed too, or it dangles as an undeclared identifier.
        edits = []
        stack = list(child.children)
        while stack:
            n = stack.pop()
            if n.type == "modifier_invocation":
                ident = next((c for c in n.children
                              if c.type == "identifier"), None)
                if ident is not None \
                        and ident.text.decode("utf-8") == base_name:
                    edits.append((n.start_byte, n.end_byte, ""))
                    continue
            stack.extend(n.children)
        return edits

    def flatten_inheritance(self, nodes_to_remove: set) -> str:
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
        edits = []
        flattened = False
        for base_name in flatten_names:
            base = contracts[base_name]
            body = self._contract_body(base)
            if body is None:
                continue
            members = [c for c in body.children
                       if c.type not in ("{", "}")
                       and not self._is_constructor(c, base_name)]
            if any(b"super." in m.text for m in members):
                continue
            member_texts = [(parsers.declaration_name(m), m.text.decode("utf-8"))
                            for m in members]
            base_parents = [self._inheritance_base(s) for s in base.children
                            if s.type == "inheritance_specifier"]
            children = [c for name, c in contracts.items()
                        if name != base_name
                        and any(s.type == "inheritance_specifier"
                                and self._inheritance_base(s) == base_name
                                for s in c.children)]
            if not children:
                continue
            for child in children:
                cbody = self._contract_body(child)
                if cbody is None:
                    continue
                existing = {parsers.declaration_name(c) for c in cbody.children
                            if c.type not in ("{", "}")}
                to_add = [txt for nm, txt in member_texts if nm not in existing]
                if to_add:
                    close = [c for c in cbody.children if c.type == "}"][-1]
                    edits.append((close.start_byte, close.start_byte,
                                  "\n" + "\n\n".join(to_add) + "\n"))
                clause = self._inheritance_clause_edit(child, base_name, base_parents)
                if clause is not None:
                    edits.append(clause)
                edits.extend(self._base_ctor_call_edits(child, base_name))
            edits.append((base.start_byte, base.end_byte, ""))
            flattened = True

        if not flattened:
            return self.content
        out = self.content
        for start, end, text in sorted(edits, key=lambda e: e[0], reverse=True):
            out = out[:start] + text + out[end:]
        return remove_empty_lines(out)
