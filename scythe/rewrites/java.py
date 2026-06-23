import re

from scythe import parsers
from scythe.rewrites.base import ASTRemoval, remove_empty_lines


class JavaDeclarationRemoval(ASTRemoval):
    LANGUAGE = "java"
    count = 0
    PLACEHOLDER_CLASS = "__A"
    CONSTANT_VALUES = {
        "int": "42", "boolean": "true", "char": "'a'", "void": "",
        "Boolean": "true", "Integer": "42", "String": "\"\"", "Object": "null",
        "double": "0.0", "float": "0.0f", "Double": "0.0", "Float": "0.0f",
        "byte": "0", "Byte": "0", "short": "0", "Short": "0",
        "long": "0L", "Long": "0L",
    }

    @classmethod
    def _const(cls, typ):
        if typ is None:
            return "null"
        base = typ.strip()
        return cls.CONSTANT_VALUES.get(base, f"(({base}) null)")

    def __init__(self, content, graph):
        super().__init__(content, graph)
        self.removed_nodes = []
        self.parser = parsers.get_parser(self.LANGUAGE)
        self._tree = None
        self.constant_values = self.CONSTANT_VALUES

    @property
    def tree(self):
        if self._tree is None:
            self._tree = self.parser.parse(self.content.encode("utf-8"))
        return self._tree

    @tree.setter
    def tree(self, value):
        self._tree = value

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
        self.removed_nodes = self.filter_enclosing_nodes(self.removed_nodes)
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
        parser = parsers.get_parser(self.LANGUAGE)
        tree = parser.parse(self.content.encode("utf-8"))
        classes = self._all_class_asts(tree.root_node)
        edits = []
        for nd in nodes_to_remove:
            class_node = classes.get(nd.name)
            if class_node is None:
                continue
            for child in class_node.children:
                if child.type in ("superclass", "super_interfaces"):
                    edits.append((child.start_byte, child.end_byte, ""))
            edits.extend(self._super_edits(class_node, classes))
        if not edits:
            return self.content
        return self._apply_edits(self.content, edits)

    def visit_super_calls(self, tree):
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
                continue
            for child in current.children:
                if child.type == "superclass" or child.type == "super_interfaces":
                    self.removed_nodes.append(child)
                    break

            self.removed_nodes.append(node)

    def visit_function_definition(self, node):
        function_name = None
        for n in node.children:
            if n.type == "identifier":
                function_name = n.text.decode("utf-8")
                break
        if any((node.name == function_name and node.node_type == "function")
               for node in self.nodes_to_remove):
            self.removed_nodes.append(node)

    def visit_call_expression(self, node):
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
        name = node_to_remove.name
        constructor_query_str = f'''(constructor_declaration name: (identifier) @ctor_name (#eq? @ctor_name "{name}")) @ctor'''
        constructor_query = parsers.JAVA_LANGUAGE.query(constructor_query_str)
        for node, capture_name in constructor_query.captures(tree.root_node):
            if capture_name == "ctor":
                self.removed_nodes.append(node)

    def remove_field(self, node_to_remove, tree):
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
        if root.id == target.id:
            return True
        for c in root.children:
            if self.node_in_subtree(target, c):
                return True
        return False


    def _decl_type(self, decl_node):
        t = decl_node.child_by_field_name("type")
        return t.text.decode("utf-8").strip() if t is not None else None

    def _value_constant(self, typ):
        if typ is None:
            return "null"
        base = typ.strip()
        if base in self.constant_values:
            return self.constant_values[base]
        return f"(({base}) null)"

    def _field_delete_range(self, field_decl, declarator):
        declarators = [c for c in field_decl.children
                       if c.type == "variable_declarator"]
        if len(declarators) <= 1:
            return (field_decl.start_byte, field_decl.end_byte)
        sibs = field_decl.children
        idx = next(i for i, c in enumerate(sibs) if c.id == declarator.id)
        start, end = declarator.start_byte, declarator.end_byte
        if idx > 0 and sibs[idx - 1].type == ",":
            start = sibs[idx - 1].start_byte
        elif idx + 1 < len(sibs) and sibs[idx + 1].type == ",":
            end = sibs[idx + 1].end_byte
        return (start, end)

    def _decls_for(self, node_to_remove, root):
        name, kind = node_to_remove.name, node_to_remove.node_type
        typ, ranges = None, []
        stack = [root]
        while stack:
            n = stack.pop()
            stack.extend(n.children)
            if kind == "function" and n.type == "method_declaration":
                nm = n.child_by_field_name("name")
                if nm is None or nm.text.decode("utf-8") != name:
                    continue
                params = n.child_by_field_name("parameters")
                if node_to_remove.args and params is not None \
                        and params.text.decode("utf-8") != node_to_remove.args:
                    continue
                typ = self._decl_type(n)
                ranges.append((n.start_byte, n.end_byte))
            elif kind == "field" and n.type == "field_declaration":
                for d in n.children:
                    if d.type != "variable_declarator":
                        continue
                    dn = d.child_by_field_name("name")
                    if dn is not None and dn.text.decode("utf-8") == name:
                        typ = self._decl_type(n)
                        ranges.append(self._field_delete_range(n, d))
            elif kind == "local_variable" \
                    and n.type == "local_variable_declaration":
                for d in n.children:
                    if d.type != "variable_declarator":
                        continue
                    dn = d.child_by_field_name("name")
                    if dn is not None and dn.text.decode("utf-8") == name:
                        typ = self._decl_type(n)
                        ranges.append((n.start_byte, n.end_byte))
        return typ, ranges

    def _use_kind(self, node):
        vdecl = self.find_ancestor(node, "variable_declarator")
        if vdecl is not None:
            nm = vdecl.child_by_field_name("name")
            if nm is not None and nm.id == node.id:
                return "decl"
        fp = self.find_ancestor(node, "formal_parameter")
        if fp is not None:
            nm = fp.child_by_field_name("name")
            if nm is not None and nm.id == node.id:
                return "decl"
        if self.find_ancestor(node, "update_expression") is not None:
            return "write"
        asg = self.find_ancestor(node, "assignment_expression")
        if asg is not None:
            left = asg.child_by_field_name("left") or (
                asg.children[0] if asg.children else None)
            if left is not None and self.node_in_subtree(node, left):
                return "write"
        return "read"

    def _enclosing_statement(self, node):
        cur = node
        while cur is not None:
            if cur.type.endswith("statement"):
                return cur
            cur = cur.parent
        return node


    def _retype_type_text(self, text, removed):
        for nm in removed:
            text = re.sub(rf"\b{re.escape(nm)}\b", self.PLACEHOLDER_CLASS, text)
        return text

    def _all_class_asts(self, root):
        out = {}
        stack = [root]
        while stack:
            n = stack.pop()
            stack.extend(n.children)
            if n.type in ("class_declaration", "interface_declaration"):
                nm = n.child_by_field_name("name")
                if nm is not None:
                    out[nm.text.decode("utf-8")] = n
        return out

    def _a_typed_names(self, root, removed):
        names = {}
        stack = [root]
        while stack:
            n = stack.pop()
            stack.extend(n.children)
            if n.type not in ("local_variable_declaration", "field_declaration",
                              "formal_parameter"):
                continue
            t = n.child_by_field_name("type")
            if t is None or t.type != "type_identifier":
                continue
            tn = t.text.decode("utf-8")
            if tn not in removed:
                continue
            if n.type == "formal_parameter":
                nm = n.child_by_field_name("name")
                if nm is not None:
                    names[nm.text.decode("utf-8")] = tn
            else:
                for d in n.children:
                    if d.type == "variable_declarator":
                        nm = d.child_by_field_name("name")
                        if nm is not None:
                            names[nm.text.decode("utf-8")] = tn
        return names

    def _receiver_class(self, recv, removed, typed_names):
        if recv is None:
            return None
        if recv.type in ("object_creation_expression", "cast_expression"):
            t = recv.child_by_field_name("type")
            if t is not None and t.text.decode("utf-8") in removed:
                return t.text.decode("utf-8")
        elif recv.type == "identifier":
            txt = recv.text.decode("utf-8")
            if txt in removed:
                return txt
            return typed_names.get(txt)
        return None

    def _used_members(self, root, removed, typed_names):
        used = {a: {"methods": set(), "fields": set()} for a in removed}
        stack = [root]
        while stack:
            n = stack.pop()
            stack.extend(n.children)
            if n.type == "method_invocation":
                a = self._receiver_class(n.child_by_field_name("object"),
                                         removed, typed_names)
                nm = n.child_by_field_name("name")
                if a is not None and nm is not None:
                    used[a]["methods"].add(nm.text.decode("utf-8"))
            elif n.type == "field_access":
                a = self._receiver_class(n.child_by_field_name("object"),
                                         removed, typed_names)
                fld = n.child_by_field_name("field")
                if a is not None and fld is not None:
                    used[a]["fields"].add(fld.text.decode("utf-8"))
        return used

    @staticmethod
    def _has_static(node):
        return any(c.type == "modifiers" and b"static" in c.text
                   for c in node.children)

    def _stub_method(self, mdecl, removed):
        nm = mdecl.child_by_field_name("name").text.decode("utf-8")
        rt_node = mdecl.child_by_field_name("type")
        rt = rt_node.text.decode("utf-8").strip() if rt_node is not None else "void"
        rt = self._retype_type_text(rt, removed)
        params = mdecl.child_by_field_name("parameters")
        ptext = self._retype_type_text(params.text.decode("utf-8"), removed) \
            if params is not None else "()"
        mods = "public static " if self._has_static(mdecl) else "public "
        body = "" if rt == "void" else f"return {self._value_constant(rt)};"
        return f"{mods}{rt} {nm}{ptext} {{ {body} }}"

    def _stub_field(self, fdecl, declarator, removed):
        t = fdecl.child_by_field_name("type")
        typ = self._retype_type_text(t.text.decode("utf-8").strip(), removed) \
            if t is not None else "Object"
        nm = declarator.child_by_field_name("name").text.decode("utf-8")
        mods = "public static " if self._has_static(fdecl) else "public "
        return f"{mods}{typ} {nm};"

    def _placeholder_members(self, removed, class_asts, used):
        seen, out = set(), []
        for a, mem in used.items():
            ast = class_asts.get(a)
            body = ast.child_by_field_name("body") if ast is not None else None
            if body is None:
                continue
            for child in body.children:
                if child.type == "method_declaration":
                    nm = child.child_by_field_name("name")
                    key = ("m", nm.text.decode("utf-8")) if nm else None
                    if key and key[1] in mem["methods"] and key not in seen:
                        seen.add(key)
                        out.append(self._stub_method(child, removed))
                elif child.type in ("field_declaration", "constant_declaration"):
                    for d in child.children:
                        if d.type != "variable_declarator":
                            continue
                        nm = d.child_by_field_name("name")
                        key = ("f", nm.text.decode("utf-8")) if nm else None
                        if key and key[1] in mem["fields"] and key not in seen:
                            seen.add(key)
                            out.append(self._stub_field(child, d, removed))
        return out

    def _inheritance_drop_edit(self, type_node):
        p = type_node.parent
        if p.type == "superclass":
            return (p.start_byte, p.end_byte, "")
        supers = [c for c in p.children
                  if c.type in ("type_identifier", "generic_type",
                                "scoped_type_identifier")]
        if len(supers) <= 1:
            clause = p.parent
            return (clause.start_byte, clause.end_byte, "")
        sibs = p.children
        idx = next((i for i, c in enumerate(sibs) if c.id == type_node.id), None)
        start, end = type_node.start_byte, type_node.end_byte
        if idx is not None and idx > 0 and sibs[idx - 1].type == ",":
            start = sibs[idx - 1].start_byte
        elif idx is not None and idx + 1 < len(sibs) and sibs[idx + 1].type == ",":
            end = sibs[idx + 1].end_byte
        return (start, end, "")

    @staticmethod
    def _generic_head(node):
        for c in node.children:
            if c.type in ("type_identifier", "scoped_type_identifier"):
                return c
        return None

    def _names_removed(self, node, removed):
        if node.type in ("type_identifier", "scoped_type_identifier"):
            return node.text.decode("utf-8") in removed
        if node.type == "generic_type":
            h = self._generic_head(node)
            return h is not None and h.text.decode("utf-8") in removed
        return False

    def _erase_type_arguments(self, ta, removed):
        args = [c for c in ta.children if c.type not in ("<", ">", ",")]
        if not args:
            return []

        def classify(a):
            if a.type == "wildcard":
                return "wild" if any(self._names_removed(c, removed)
                                     for c in a.children) else None
            return "arg" if self._names_removed(a, removed) else None

        flags = [classify(a) for a in args]
        if all(f == "arg" for f in flags):
            return [(ta.start_byte, ta.end_byte, "")]
        edits, sibs = [], ta.children
        for a, f in zip(args, flags):
            if f is None:
                continue
            if f == "wild":
                edits.append((a.start_byte, a.end_byte, "?"))
                continue
            idx = next((i for i, c in enumerate(sibs) if c.id == a.id), None)
            start, end = a.start_byte, a.end_byte
            if idx is not None and idx > 0 and sibs[idx - 1].type == ",":
                start = sibs[idx - 1].start_byte
            elif idx is not None and idx + 1 < len(sibs) and sibs[idx + 1].type == ",":
                end = sibs[idx + 1].end_byte
            edits.append((start, end, ""))
        return edits

    def _erase_references(self, removed, root):
        edits = []
        stack = [root]
        while stack:
            n = stack.pop()
            stack.extend(n.children)
            t = n.type
            if t == "object_creation_expression":
                ty = n.child_by_field_name("type")
                if ty is not None and self._names_removed(ty, removed):
                    edits.append((n.start_byte, n.end_byte,
                                  f"(({self.PLACEHOLDER_CLASS}) null)"))
                continue
            if t == "type_arguments":
                edits.extend(self._erase_type_arguments(n, removed))
                continue
            if t == "type_identifier" and n.text.decode("utf-8") in removed:
                ref = (n.parent if n.parent is not None
                       and n.parent.type == "generic_type" else n)
                p = ref.parent
                pt = p.type if p is not None else None
                if pt in ("superclass", "type_list"):
                    edits.append(self._inheritance_drop_edit(ref))
                elif pt == "type_bound":
                    edits.append((p.start_byte, p.end_byte, ""))
                elif pt == "wildcard":
                    edits.append((p.start_byte, p.end_byte, "?"))
                elif pt == "type_arguments":
                    pass
                else:
                    edits.append((ref.start_byte, ref.end_byte,
                                  self.PLACEHOLDER_CLASS))
        return edits

    def _class_placeholder_edits(self, removed, root):
        edits = []
        class_asts = self._all_class_asts(root)
        for a in removed:
            ast = class_asts.get(a)
            if ast is not None:
                edits.append((ast.start_byte, ast.end_byte, ""))

        edits.extend(self._erase_references(removed, root))

        for cls_node in class_asts.values():
            _, base_name, _ = self._superclass_info(cls_node)
            if base_name is not None and base_name in removed:
                edits.extend(self._super_edits(cls_node, class_asts))

        typed_names = self._a_typed_names(root, removed)
        used = self._used_members(root, removed, typed_names)
        members = self._placeholder_members(removed, class_asts, used)

        append_text = ""
        existing = class_asts.get(self.PLACEHOLDER_CLASS)
        if existing is not None:
            body = existing.child_by_field_name("body")
            prior = [self._retype_type_text(c.text.decode("utf-8"), removed)
                     for c in (body.children if body else [])
                     if c.type in ("method_declaration", "field_declaration",
                                   "constant_declaration")]
            all_members = list(dict.fromkeys(prior + members))
            decl = "class %s {\n  %s\n}" % (
                self.PLACEHOLDER_CLASS, "\n  ".join(all_members))
            edits.append((existing.start_byte, existing.end_byte, decl))
        else:
            decl = "class %s {\n  %s\n}" % (
                self.PLACEHOLDER_CLASS, "\n  ".join(members))
            append_text = "\n" + decl + "\n"
        return edits, append_text


    @staticmethod
    def _class_body(node):
        return node.child_by_field_name("body")

    def _member_name(self, m):
        if m.type in ("method_declaration", "constructor_declaration",
                      "class_declaration", "interface_declaration",
                      "enum_declaration"):
            nm = m.child_by_field_name("name")
            return nm.text.decode("utf-8") if nm is not None else None
        if m.type in ("field_declaration", "constant_declaration"):
            for d in m.children:
                if d.type == "variable_declarator":
                    nm = d.child_by_field_name("name")
                    return nm.text.decode("utf-8") if nm is not None else None
        return None

    def _is_abstract_method(self, m):
        if m.type != "method_declaration":
            return False
        if m.child_by_field_name("body") is None:
            return True
        return any(c.type == "modifiers" and b"abstract" in c.text
                   for c in m.children)

    @staticmethod
    def _has_type_params(node):
        return any(c.type == "type_parameters" for c in node.children)

    def _superclass_info(self, class_node):
        sc = class_node.child_by_field_name("superclass")
        if sc is None:
            return (None, None, False)
        typ = next((c for c in sc.children
                    if c.type in ("type_identifier", "generic_type",
                                  "scoped_type_identifier")), None)
        if typ is None:
            return (sc, None, False)
        if typ.type == "type_identifier":
            return (sc, typ.text.decode("utf-8"), False)
        inner = next((c for c in typ.children if c.type == "type_identifier"), None)
        return (sc, inner.text.decode("utf-8") if inner is not None else None, True)

    def _override_annotations(self, node):
        out, stack = [], [node]
        while stack:
            n = stack.pop()
            stack.extend(n.children)
            if n.type in ("marker_annotation", "annotation"):
                nm = n.child_by_field_name("name")
                if nm is not None and nm.text.decode("utf-8") == "Override":
                    out.append(n)
        return out

    def _stmt_of(self, node):
        cur = node
        while cur is not None:
            if cur.type.endswith("statement") or cur.type in (
                    "local_variable_declaration", "field_declaration"):
                return cur
            cur = cur.parent
        return node

    def _enclosing_class(self, node):
        cur = node.parent
        while cur is not None:
            if cur.type in ("class_declaration", "interface_declaration"):
                return cur
            cur = cur.parent
        return None

    def _member_type_in(self, class_ast, name, kind):
        body = class_ast.child_by_field_name("body") if class_ast is not None else None
        if body is None:
            return None
        for c in body.children:
            if kind == "method" and c.type == "method_declaration":
                nm = c.child_by_field_name("name")
                if nm is not None and nm.text.decode("utf-8") == name:
                    t = c.child_by_field_name("type")
                    return t.text.decode("utf-8").strip() if t is not None else "void"
            elif kind == "field" and c.type in ("field_declaration",
                                                 "constant_declaration"):
                for d in c.children:
                    if d.type == "variable_declarator":
                        nm = d.child_by_field_name("name")
                        if nm is not None and nm.text.decode("utf-8") == name:
                            t = c.child_by_field_name("type")
                            return t.text.decode("utf-8").strip() \
                                if t is not None else None
        return None

    def _super_member_type(self, node, name, kind, classes):
        cls = self._enclosing_class(node)
        if cls is None:
            return None
        _, sup_name, _ = self._superclass_info(cls)
        if sup_name is None:
            return None
        return self._member_type_in(classes.get(sup_name), name, kind)

    def _super_edits(self, scope, classes):
        edits, stack = [], [scope]
        while stack:
            n = stack.pop()
            stack.extend(n.children)
            if n.type != "super":
                continue
            p = n.parent
            if p is None:
                continue
            if p.type == "explicit_constructor_invocation":
                edits.append((p.start_byte, p.end_byte, ""))
            elif p.type == "method_invocation":
                if p.parent is not None and p.parent.type == "expression_statement":
                    edits.append((p.parent.start_byte, p.parent.end_byte, ""))
                else:
                    nm = p.child_by_field_name("name")
                    rt = self._super_member_type(
                        p, nm.text.decode("utf-8") if nm else "", "method", classes)
                    edits.append((p.start_byte, p.end_byte, self._value_constant(rt)))
            elif p.type == "field_access":
                if self._use_kind(p) == "read":
                    fld = p.child_by_field_name("field")
                    ft = self._super_member_type(
                        p, fld.text.decode("utf-8") if fld else "", "field", classes)
                    edits.append((p.start_byte, p.end_byte, self._value_constant(ft)))
                else:
                    st = self._enclosing_statement(p)
                    edits.append((st.start_byte, st.end_byte, ""))
            else:
                st = self._stmt_of(n)
                edits.append((st.start_byte, st.end_byte, ""))
        return edits

    def _clean_super(self, member, classes):
        base = member.start_byte
        rel = [(s - base, e - base, txt)
               for (s, e, txt) in self._super_edits(member, classes)]
        return self._apply_edits(member.text.decode("utf-8"), rel)

    def _apply_edits(self, content, edits):
        uniq = {}
        for s, e, txt in edits:
            uniq.setdefault((s, e), txt)
        spans = list(uniq)
        kept = [(s, e, uniq[(s, e)]) for (s, e) in spans
                if s == e or not any(os <= s and e <= oe and (os, oe) != (s, e)
                                     for (os, oe) in spans)]
        src = bytearray(content.encode("utf-8"))
        for s, e, txt in sorted(kept, key=lambda x: x[0], reverse=True):
            src[s:e] = txt.encode("utf-8")
        return src.decode("utf-8")

    def flatten_inheritance(self, nodes_to_remove: set):
        parser = parsers.get_parser(self.LANGUAGE)
        tree = parser.parse(self.content.encode("utf-8"))
        classes = self._all_class_asts(tree.root_node)
        flatten_names = {n.name for n in nodes_to_remove
                         if n.node_type == "class" and n.name in classes}

        edits = []
        flattened = False
        for base_name in flatten_names:
            base = classes[base_name]
            if base.type != "class_declaration" or self._has_type_params(base):
                continue
            body = self._class_body(base)
            if body is None:
                continue
            _, base_super, base_super_generic = self._superclass_info(base)
            if base_super is not None and base_super_generic:
                continue

            members = []
            for m in body.children:
                if m.type == "constructor_declaration":
                    continue
                if m.type not in ("method_declaration", "field_declaration",
                                  "constant_declaration", "class_declaration",
                                  "interface_declaration", "enum_declaration"):
                    continue
                if self._is_abstract_method(m):
                    continue
                members.append(m)

            member_texts = [(self._member_name(m), self._clean_super(m, classes))
                            for m in members]
            children = [c for nm, c in classes.items()
                        if nm != base_name
                        and self._superclass_info(c)[1] == base_name]
            if not children:
                continue

            child_edits, ok = [], True
            for child in children:
                _, _, child_super_generic = self._superclass_info(child)
                if child_super_generic:
                    ok = False
                    break
                cbody = self._class_body(child)
                if cbody is None:
                    ok = False
                    break
                existing = {self._member_name(m) for m in cbody.children}
                to_add = [txt for nm, txt in member_texts if nm not in existing]
                closes = [c for c in cbody.children if c.type == "}"]
                if to_add and closes:
                    pos = closes[-1].start_byte
                    child_edits.append((pos, pos, "\n" + "\n\n".join(to_add) + "\n"))
                for ann in self._override_annotations(child):
                    child_edits.append((ann.start_byte, ann.end_byte, ""))
                child_edits.extend(self._super_edits(child, classes))
                scn = self._superclass_info(child)[0]
                if scn is not None:
                    repl = f"extends {base_super}" if base_super else ""
                    child_edits.append((scn.start_byte, scn.end_byte, repl))
            if not ok:
                continue

            edits.extend(child_edits)
            edits.append((base.start_byte, base.end_byte, ""))
            flattened = True

        if not flattened:
            return self.content
        return remove_empty_lines(self._apply_edits(self.content, edits))

    def replace_nodes(self, nodes_to_remove: set):
        parser = parsers.get_parser(self.LANGUAGE)
        tree = parser.parse(self.content.encode("utf-8"))
        root = tree.root_node
        self.nodes_to_remove = nodes_to_remove

        func_types, value_types = {}, {}
        edits = []
        for nd in nodes_to_remove:
            if nd.node_type not in ("function", "field", "local_variable"):
                continue
            typ, ranges = self._decls_for(nd, root)
            if not ranges:
                continue
            (func_types if nd.node_type == "function" else value_types)[nd.name] = typ
            edits.extend((s, e, "") for (s, e) in ranges)

        stack = [root]
        while stack:
            n = stack.pop()
            stack.extend(n.children)

            if n.type == "method_invocation":
                nm = n.child_by_field_name("name")
                name = nm.text.decode("utf-8") if nm is not None else None
                if name in func_types:
                    if n.parent is not None \
                            and n.parent.type == "expression_statement":
                        edits.append((n.parent.start_byte,
                                      n.parent.end_byte, ""))
                    else:
                        edits.append((n.start_byte, n.end_byte,
                                      self._value_constant(func_types[name])))
                continue

            if n.type == "field_access":
                fld = n.child_by_field_name("field")
                name = fld.text.decode("utf-8") if fld is not None else None
                if name in value_types:
                    kind = self._use_kind(n)
                    if kind == "write":
                        st = self._enclosing_statement(n)
                        edits.append((st.start_byte, st.end_byte, ""))
                    elif kind == "read":
                        edits.append((n.start_byte, n.end_byte,
                                      self._value_constant(value_types[name])))
                continue

            if n.type == "identifier":
                name = n.text.decode("utf-8")
                if name not in value_types:
                    continue
                p = n.parent
                if p is not None:
                    fld = p.child_by_field_name("field")
                    nmc = p.child_by_field_name("name")
                    if (fld is not None and fld.id == n.id) \
                            or (p.type == "method_invocation" and nmc is not None
                                and nmc.id == n.id) \
                            or p.type == "scoped_identifier":
                        continue
                if self.find_ancestor(n, "import_declaration") is not None \
                        or self.find_ancestor(n, "package_declaration") is not None:
                    continue
                kind = self._use_kind(n)
                if kind == "write":
                    st = self._enclosing_statement(n)
                    edits.append((st.start_byte, st.end_byte, ""))
                elif kind == "read":
                    edits.append((n.start_byte, n.end_byte,
                                  self._value_constant(value_types[name])))

        append_text = ""
        class_names = {nd.name for nd in nodes_to_remove
                       if nd.node_type == "class"
                       and nd.name != self.PLACEHOLDER_CLASS}
        if class_names:
            c_edits, append_text = self._class_placeholder_edits(class_names, root)
            edits.extend(c_edits)

        if not edits and not append_text:
            return self.content

        uniq = {}
        for s, e, txt in edits:
            uniq.setdefault((s, e), txt)
        spans = list(uniq)
        kept = [(s, e, uniq[(s, e)]) for (s, e) in spans
                if not any(os <= s and e <= oe and (os, oe) != (s, e)
                           for (os, oe) in spans)]
        source = bytearray(self.content.encode("utf-8"))
        for s, e, txt in sorted(kept, key=lambda x: x[0], reverse=True):
            source[s:e] = txt.encode("utf-8")
        result = source.decode("utf-8") + append_text
        self.content = result
        return result

    def replacement_table(self):
        root = self.tree.root_node
        method_decls, field_decls, local_decls = {}, {}, {}
        func_use, value_use = {}, {}

        stack = [root]
        while stack:
            n = stack.pop()
            stack.extend(n.children)
            if n.type == "method_declaration":
                nm = n.child_by_field_name("name")
                if nm is not None:
                    params = n.child_by_field_name("parameters")
                    args = (params.text.decode("utf-8")
                            if params is not None else None)
                    method_decls.setdefault(nm.text.decode("utf-8"), []).append(
                        (args, self._decl_type(n), (n.start_byte, n.end_byte)))
            elif n.type == "field_declaration":
                for d in n.children:
                    if d.type != "variable_declarator":
                        continue
                    dn = d.child_by_field_name("name")
                    if dn is not None:
                        field_decls.setdefault(
                            dn.text.decode("utf-8"), []).append(
                            (self._decl_type(n), self._field_delete_range(n, d)))
            elif n.type == "local_variable_declaration":
                for d in n.children:
                    if d.type != "variable_declarator":
                        continue
                    dn = d.child_by_field_name("name")
                    if dn is not None:
                        local_decls.setdefault(
                            dn.text.decode("utf-8"), []).append(
                            (self._decl_type(n), (n.start_byte, n.end_byte)))

        stack = [root]
        while stack:
            n = stack.pop()
            stack.extend(n.children)
            if n.type == "method_invocation":
                nm = n.child_by_field_name("name")
                if nm is not None:
                    name = nm.text.decode("utf-8")
                    if n.parent is not None \
                            and n.parent.type == "expression_statement":
                        func_use.setdefault(name, []).append(
                            (n.parent.start_byte, n.parent.end_byte, "delete"))
                    else:
                        func_use.setdefault(name, []).append(
                            (n.start_byte, n.end_byte, "value"))
                continue
            if n.type == "field_access":
                fld = n.child_by_field_name("field")
                if fld is not None:
                    name = fld.text.decode("utf-8")
                    kind = self._use_kind(n)
                    if kind == "write":
                        st = self._enclosing_statement(n)
                        value_use.setdefault(name, []).append(
                            (st.start_byte, st.end_byte, "delete"))
                    elif kind == "read":
                        value_use.setdefault(name, []).append(
                            (n.start_byte, n.end_byte, "value"))
                continue
            if n.type == "identifier":
                name = n.text.decode("utf-8")
                p = n.parent
                if p is not None:
                    fld = p.child_by_field_name("field")
                    nmc = p.child_by_field_name("name")
                    if (fld is not None and fld.id == n.id) \
                            or (p.type == "method_invocation" and nmc is not None
                                and nmc.id == n.id) \
                            or p.type == "scoped_identifier":
                        continue
                if self.find_ancestor(n, "import_declaration") is not None \
                        or self.find_ancestor(n, "package_declaration") is not None:
                    continue
                kind = self._use_kind(n)
                if kind == "write":
                    st = self._enclosing_statement(n)
                    value_use.setdefault(name, []).append(
                        (st.start_byte, st.end_byte, "delete"))
                elif kind == "read":
                    value_use.setdefault(name, []).append(
                        (n.start_byte, n.end_byte, "value"))
        return {"method_decls": method_decls, "field_decls": field_decls,
                "local_decls": local_decls, "func_use": func_use,
                "value_use": value_use}

    @classmethod
    def assemble_from_table(cls, content, sel, table):
        func_types, value_types = {}, {}
        edits = []
        for nd in sel:
            if nd.node_type == "function":
                ms = [m for m in table["method_decls"].get(nd.name, [])
                      if not (nd.args and m[0] is not None and m[0] != nd.args)]
                if not ms:
                    continue
                func_types[nd.name] = ms[-1][1]
                edits.extend((s, e, "") for (_, _, (s, e)) in ms)
            elif nd.node_type == "field":
                ms = table["field_decls"].get(nd.name, [])
                if not ms:
                    continue
                value_types[nd.name] = ms[-1][0]
                edits.extend((s, e, "") for (_, (s, e)) in ms)
            elif nd.node_type == "local_variable":
                ms = table["local_decls"].get(nd.name, [])
                if not ms:
                    continue
                value_types[nd.name] = ms[-1][0]
                edits.extend((s, e, "") for (_, (s, e)) in ms)
        for name, typ in func_types.items():
            for (s, e, kind) in table["func_use"].get(name, []):
                edits.append((s, e, "") if kind == "delete"
                             else (s, e, cls._const(typ)))
        for name, typ in value_types.items():
            for (s, e, kind) in table["value_use"].get(name, []):
                edits.append((s, e, "") if kind == "delete"
                             else (s, e, cls._const(typ)))
        if not edits:
            return content
        uniq = {}
        for s, e, txt in edits:
            uniq.setdefault((s, e), txt)
        spans = list(uniq)
        kept = [(s, e, uniq[(s, e)]) for (s, e) in spans
                if not any(os <= s and e <= oe and (os, oe) != (s, e)
                           for (os, oe) in spans)]
        source = bytearray(content.encode("utf-8"))
        for s, e, txt in sorted(kept, key=lambda x: x[0], reverse=True):
            source[s:e] = txt.encode("utf-8")
        return source.decode("utf-8")

    @classmethod
    def build_candidate(cls, base_content, graph, removed, mode, table=None):
        sel = set(removed)
        if mode == "replacement" and sel and all(
                n.node_type in ("function", "field", "local_variable")
                for n in sel):
            if table is None:
                table = cls(base_content, graph).replacement_table()
            return cls.assemble_from_table(base_content, sel, table), table
        return cls._slow_candidate(base_content, graph, sel, mode), table
