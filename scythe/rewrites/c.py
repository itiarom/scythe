import traceback
from typing import Any

from scythe import parsers
from scythe.rewrites.base import ASTRemoval, remove_empty_lines


class CDeclarationRemoval(ASTRemoval):
    LANGUAGE = "c"

    CONSTANT_VALUES = {
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

    def __init__(self, content, graph):
        super().__init__(content, graph)
        self.removed_nodes = []
        self.removed_declarations = []
        self.goto_statements = []
        self.replaced_assignment_declarations = []
        self.removed_nodes_with_types = {}
        self.constant_values = self.CONSTANT_VALUES

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
        for child in node.children:
            if child.type == "argument_list":
                self._handle_call_expression_argument_list(child, node, None)
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


    @classmethod
    def _const(cls, typ):
        if typ is None:
            return "NULL"
        return cls.CONSTANT_VALUES.get(typ.strip(), "0")

    def _c_find_ancestor(self, node, typ):
        cur = node.parent
        while cur is not None:
            if cur.type == typ:
                return cur
            cur = cur.parent
        return None

    def _c_enclosing_statement(self, node):
        cur = node
        while cur is not None:
            if cur.type.endswith("statement") or cur.type == "declaration":
                return cur
            cur = cur.parent
        return node

    def _c_use_kind(self, node):
        if self._c_find_ancestor(node, "update_expression") is not None:
            return "write"
        asg = self._c_find_ancestor(node, "assignment_expression")
        if asg is not None:
            left = asg.child_by_field_name("left") or (
                asg.children[0] if asg.children else None)
            if left is not None and \
                    left.start_byte <= node.start_byte \
                    and node.end_byte <= left.end_byte:
                return "write"
        return "read"

    def _c_type_text(self, node):
        for c in node.children:
            if c.type in ("function_declarator", "init_declarator",
                          "array_declarator", "pointer_declarator", ";"):
                break
            if c.type in ("primitive_type", "sized_type_specifier",
                          "type_identifier", "struct_specifier",
                          "union_specifier", "enum_specifier"):
                return c.text.decode("utf-8")
        return None

    def _c_func_name(self, defn):
        for c in defn.children:
            if c.type != "function_declarator":
                continue
            for cc in c.children:
                if cc.type == "identifier":
                    return cc.text.decode("utf-8")
                if cc.type == "parenthesized_declarator":
                    for x in cc.children:
                        if x.type == "identifier":
                            return x.text.decode("utf-8")
        return None

    def _c_declared_names(self, decl):
        names = []

        def walk(n):
            for c in n.children:
                if c.type == "identifier":
                    names.append(c.text.decode("utf-8"))
                elif c.type in ("init_declarator", "array_declarator",
                                "pointer_declarator", "function_declarator"):
                    walk(c)
        walk(decl)
        return names

    def _c_callee_node(self, call):
        n = call.child_by_field_name("function")
        while n is not None and n.type == "parenthesized_expression":
            inner = [c for c in n.children if c.type not in ("(", ")")]
            n = inner[0] if inner else None
        return n if (n is not None and n.type == "identifier") else None

    def _c_delete_text(self, stmt):
        # Deleting a statement that is the sole (unbraced) body of a control
        # structure would leave a dangling `if (..)`/`for (..)`; an empty
        # statement is valid there, so only delete to nothing inside a block.
        p = stmt.parent
        if p is not None and p.type in (
                "compound_statement", "translation_unit", "declaration_list"):
            return ""
        return ";"

    def _c_is_prototype(self, decl):
        stack = list(decl.children)
        while stack:
            c = stack.pop()
            if c.type == "function_declarator":
                return True
            if c.type in ("pointer_declarator", "init_declarator",
                          "array_declarator", "parenthesized_declarator"):
                stack.extend(c.children)
        return False

    def replacement_table(self):
        root = parsers.get_parser(self.LANGUAGE).parse(
            self.content.encode("utf-8")).root_node
        func_decls, global_decls = {}, {}
        stack = [root]
        while stack:
            n = stack.pop()
            stack.extend(n.children)
            if n.type == "function_definition":
                name = self._c_func_name(n)
                if name is None:
                    continue
                rtype = self._c_type_text(n)
                if name == "main":
                    body = n.child_by_field_name("body")
                    spans = ([(c.start_byte, c.end_byte) for c in body.children
                              if c.type not in ("{", "}")]
                             if body is not None else [])
                else:
                    spans = [(n.start_byte, n.end_byte)]
                func_decls.setdefault(name, []).append((rtype, spans))
            elif n.type == "declaration":
                if not any(c.type == "storage_class_specifier"
                           and c.text.decode("utf-8") == "static"
                           for c in n.children):
                    continue
                if self._c_is_prototype(n):
                    continue
                typ = self._c_type_text(n)
                for nm in self._c_declared_names(n):
                    global_decls.setdefault(nm, []).append(
                        (typ, (n.start_byte, n.end_byte)))

        # A name that is both a function and a "global" is a function (e.g. a
        # static prototype slipped through); never rewrite its uses as a value.
        global_names = set(global_decls) - set(func_decls)
        func_use, value_use, callee_ids = {}, {}, set()
        stack = [root]
        while stack:
            n = stack.pop()
            stack.extend(n.children)
            if n.type == "call_expression":
                callee = self._c_callee_node(n)
                if callee is not None:
                    callee_ids.add(callee.id)
                    name = callee.text.decode("utf-8")
                    p = n.parent
                    if p is not None and p.type == "expression_statement":
                        func_use.setdefault(name, []).append(
                            (p.start_byte, p.end_byte, self._c_delete_text(p)))
                    else:
                        func_use.setdefault(name, []).append(
                            (n.start_byte, n.end_byte, None))
            elif n.type == "identifier":
                if n.id in callee_ids:
                    continue
                name = n.text.decode("utf-8")
                if name not in global_names:
                    continue
                if self._c_use_kind(n) == "write":
                    st = self._c_enclosing_statement(n)
                    value_use.setdefault(name, []).append(
                        (st.start_byte, st.end_byte, self._c_delete_text(st)))
                else:
                    value_use.setdefault(name, []).append(
                        (n.start_byte, n.end_byte, None))
        return {"func_decls": func_decls, "global_decls": global_decls,
                "func_use": func_use, "value_use": value_use}

    @classmethod
    def assemble_from_table(cls, content, sel, table):
        func_types, value_types, edits = {}, {}, []
        for nd in sel:
            if nd.node_type == "function":
                entries = table["func_decls"].get(nd.name, [])
                if not entries:
                    continue
                func_types[nd.name] = entries[-1][0]
                for (_, spans) in entries:
                    edits.extend((s, e, "") for (s, e) in spans)
            elif nd.node_type == "global_variable":
                entries = table["global_decls"].get(nd.name, [])
                if not entries:
                    continue
                value_types[nd.name] = entries[-1][0]
                edits.extend((s, e, "") for (_, (s, e)) in entries)
        for name, typ in func_types.items():
            for (s, e, repl) in table["func_use"].get(name, []):
                edits.append((s, e, cls._const(typ) if repl is None else repl))
        for name, typ in value_types.items():
            for (s, e, repl) in table["value_use"].get(name, []):
                edits.append((s, e, cls._const(typ) if repl is None else repl))
        return cls._splice_edits(content, edits)

    @classmethod
    def _splice_edits(cls, content, edits):
        if not edits:
            return remove_empty_lines(content)
        uniq = {}
        for s, e, txt in edits:
            uniq.setdefault((s, e), txt)
        enc = content.encode("utf-8")
        out, prev, cur_end = [], 0, -1
        for (s, e) in sorted(uniq, key=lambda se: (se[0], -se[1])):
            if s < cur_end and e <= cur_end:
                continue
            out.append(enc[prev:s])
            txt = uniq[(s, e)]
            if txt:
                out.append(txt.encode("utf-8"))
            prev, cur_end = e, max(cur_end, e)
        out.append(enc[prev:])
        return remove_empty_lines(b"".join(out).decode("utf-8"))

    @classmethod
    def build_candidate(cls, base_content, graph, removed, mode, table=None):
        sel = set(removed)
        if mode == "replacement" and sel and all(
                n.node_type in ("function", "global_variable") for n in sel):
            if table is None:
                table = cls(base_content, graph).replacement_table()
            return cls.assemble_from_table(base_content, sel, table), table
        if sel and all(n.node_type in ("for_statement", "if_statement",
                                       "initializer", "expression") for n in sel):
            edits = [cls._fragment_edit(n) for n in sel]
            return cls._splice_edits(base_content, edits), table
        if sel and all(n.node_type == "struct" for n in sel):
            return cls(base_content, graph).remove_structs(sel), table
        return cls._slow_candidate(base_content, graph, sel, mode), table

    @classmethod
    def _fragment_edit(cls, n):
        # repl is a property of the fragment kind, not stored in the graph:
        # collapse an initializer/expression to a constant, delete a block ("" in
        # a block, ";" for an unbraced control body — args[2] is in_block).
        if n.node_type == "initializer":
            repl = "{0}"
        elif n.node_type == "expression":
            repl = "0xDEADBEEF"
        else:
            repl = "" if n.args[2] else ";"
        return (n.args[0], n.args[1], repl)

    def _c_struct_field_types(self, struct_node):
        out = {}
        for fl in struct_node.children:
            if fl.type != "field_declaration_list":
                continue
            for fd in fl.children:
                if fd.type != "field_declaration":
                    continue
                ftype = self._c_type_text(fd)
                stack = list(fd.children)
                while stack:
                    n = stack.pop()
                    if n.type == "field_identifier":
                        out[n.text.decode("utf-8")] = ftype
                    elif n.type in ("array_declarator", "pointer_declarator"):
                        stack.extend(n.children)
        return out

    def _c_struct_type_of(self, decl, names):
        for c in decl.children:
            if c.type == "struct_specifier":
                ti = [x for x in c.children if x.type == "type_identifier"]
                if ti and ti[0].text.decode("utf-8") in names:
                    return ti[0].text.decode("utf-8")
        return None

    def _c_leftmost_id(self, node):
        n = node
        while n is not None:
            if n.type == "identifier":
                return n.text.decode("utf-8")
            nxt = n.child_by_field_name("argument") \
                or n.child_by_field_name("declarator")
            if nxt is None:
                kids = [c for c in n.children if c.type not in (
                    "(", ")", "[", "]", "*", ".", "->")]
                nxt = kids[0] if kids else None
            if nxt is None or nxt is n:
                return None
            n = nxt
        return None

    def _c_is_assign_target(self, node):
        # True iff `node` is the lvalue being written: an ++/-- operand, or on the
        # lvalue spine of an assignment's left (x.f in `x.f=e`, or x.f in
        # `x.f[i]=e`) — but NOT an index/operand inside the LHS (the x.f in
        # `a[x.f]=e` is a READ).
        p = node.parent
        if p is not None and p.type == "update_expression":
            return True
        asg = self._c_find_ancestor(node, "assignment_expression")
        if asg is None:
            return False
        cur, seen = asg.child_by_field_name("left"), 0
        while cur is not None and seen < 32:
            seen += 1
            if cur.start_byte == node.start_byte \
                    and cur.end_byte == node.end_byte:
                return True
            if cur.type in ("field_expression", "subscript_expression",
                            "pointer_expression"):
                cur = cur.child_by_field_name("argument")
            elif cur.type == "parenthesized_expression":
                inner = [c for c in cur.children if c.type not in ("(", ")")]
                cur = inner[0] if inner else None
            else:
                break
        return False

    PLACEHOLDER_STRUCT = "__A"

    def remove_structs(self, struct_sel):
        # Remove the selected struct definitions entirely, introduce one empty
        # placeholder `struct __A {}`, and rewrite every usage of a removed
        # struct type to `struct __A`. __A has no members, so drop the aggregate
        # initializers of removed-struct variables and replace field accesses
        # with a constant of the field's type (reads and writes alike: `x.f`,
        # `x.f = e`, `x.f++` all become the constant). Variables of a removed
        # struct type are matched precisely, so other structs' same-named fields
        # are untouched.
        names = {n.name for n in struct_sel
                 if n.name != self.PLACEHOLDER_STRUCT}
        if not names:
            return self.content
        root = parsers.get_parser(self.LANGUAGE).parse(
            self.content.encode("utf-8")).root_node
        field_type, svars, edits = {}, set(), []
        def_spans, rename_ti = [], []
        stack = [root]
        while stack:
            n = stack.pop()
            stack.extend(n.children)
            if n.type == "struct_specifier":
                ti = next((c for c in n.children
                           if c.type == "type_identifier"), None)
                if ti is None or ti.text.decode("utf-8") not in names:
                    continue
                body = next((c for c in n.children
                             if c.type == "field_declaration_list"), None)
                if body is None:
                    rename_ti.append(ti)
                elif n.parent is not None \
                        and n.parent.type == "translation_unit":
                    field_type.update(self._c_struct_field_types(n))
                    end = n.end_byte
                    sib = n.next_sibling
                    if sib is not None and sib.type == ";":
                        end = sib.end_byte
                    def_spans.append((n.start_byte, end))
            elif n.type in ("declaration", "parameter_declaration") \
                    and self._c_struct_type_of(n, names):
                for nm in self._c_declared_names(n):
                    svars.add(nm)
                for c in n.children:
                    if c.type != "init_declarator":
                        continue
                    decl = c.child_by_field_name("declarator")
                    val = c.child_by_field_name("value")
                    if decl is not None and val is not None \
                            and val.type == "initializer_list":
                        edits.append((decl.end_byte, val.end_byte, ""))
        if not def_spans:
            return self.content

        for ti in rename_ti:
            edits.append((ti.start_byte, ti.end_byte,
                          self.PLACEHOLDER_STRUCT))
        placeholder_exists = f"struct {self.PLACEHOLDER_STRUCT}" in self.content
        for i, (s, e) in enumerate(sorted(def_spans)):
            if i == 0 and not placeholder_exists:
                edits.append((s, e, f"struct {self.PLACEHOLDER_STRUCT} {{}};"))
            else:
                edits.append((s, e, ""))

        stack = [root]
        while stack:
            n = stack.pop()
            stack.extend(n.children)
            if n.type != "field_expression":
                continue
            fld = n.child_by_field_name("field")
            base = n.child_by_field_name("argument")
            if fld is None or base is None:
                continue
            fname = fld.text.decode("utf-8")
            if fname not in field_type \
                    or self._c_leftmost_id(base) not in svars:
                continue
            const = self._const(field_type[fname])
            if self._c_is_assign_target(n):
                p = n.parent
                if p is not None and p.type == "update_expression":
                    edits.append((p.start_byte, p.end_byte, const))
                else:
                    asg = self._c_find_ancestor(n, "assignment_expression")
                    if asg is not None:
                        edits.append((asg.start_byte, asg.end_byte, const))
            else:
                edits.append((n.start_byte, n.end_byte, const))
        return self._splice_edits(self.content, edits)

    def remove_nodes(self, nodes_to_remove: set, mode: str) -> str:
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
        # Slice the exact bytes that were parsed (node offsets index into these);
        # tree.root_node.text starts at the first token, so it would shift every
        # cut when the source has leading whitespace/comments.
        modified_code = self.content.encode("utf-8")
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
            if "new_text" in edit and mode in ["replacement", "combination"]:
                tree.edit(
                    start_byte=edit["start_byte"],
                    old_end_byte=edit["old_end_byte"],
                    new_end_byte=edit["new_end_byte_with_constant"],
                    start_point=edit["start_point"],
                    old_end_point=edit["old_end_point"],
                    new_end_point=edit["new_end_point_with_constant"],
                )
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
                modified_code = (
                    modified_code[: edit["start_byte"]] +
                    modified_code[edit["start_byte"]:edit["new_end_byte"]] +
                    modified_code[edit["old_end_byte"]:]
                )

        parser = parsers.get_parser(self.LANGUAGE)
        updated_tree = parser.parse(modified_code, tree)
        return remove_empty_lines(updated_tree.root_node.text.decode("utf-8"))
