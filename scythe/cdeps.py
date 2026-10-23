"""Typed dependency graph of a C program: each use of a declaration records the
rewrite that detaches it, and `CModel.assemble` applies them for removed nodes."""
import bisect
import re

import networkx as nx

from scythe import parsers
from scythe.graph import DeclarationNode
from scythe.utils import remove_empty_lines

BLOCKS = ("compound_statement", "translation_unit", "declaration_list",
          "field_declaration_list")
NAME_TYPES = ("identifier", "type_identifier", "primitive_type")
SIDE_EFFECTS = ("call_expression", "assignment_expression", "update_expression")
LITERALS = ("number_literal", "char_literal", "true", "false")
MAX_RENDER_DEPTH = 400
TYPE_NODES = ("primitive_type", "sized_type_specifier", "type_identifier",
              "struct_specifier", "union_specifier", "enum_specifier")
FRAGMENT_MIN_BYTES = 40
EXPRESSIONS = ("binary_expression", "call_expression", "conditional_expression",
               "parenthesized_expression")

INT_DEFAULTS = {
    "int": "0xDEADBEEF", "short": "0xDEAD", "long": "0xDEADBEEFDEADBEEF",
    "long long": "0xDEADBEEFDEADBEEF", "unsigned int": "0xDEADBEEFU",
    "unsigned short": "0xDEADU", "unsigned long": "0xDEADBEEFDEADBEEFUL",
    "unsigned long long": "0xDEADBEEFDEADBEEFULL", "char": "'X'",
    "unsigned char": "'X'", "signed char": "'X'", "size_t": "0xDEADBEEF",
    "int8_t": "0xDE", "uint8_t": "0xDEU", "int16_t": "0xDEAD",
    "uint16_t": "0xDEADU", "int32_t": "0xDEADBEEF", "uint32_t": "0xDEADBEEFU",
    "int64_t": "0xDEADBEEFDEADBEEF", "uint64_t": "0xDEADBEEFDEADBEEFULL",
}
_QUALIFIERS = re.compile(r"\b(const|volatile|static|register|extern|inline)\b")


def text(node):
    return node.text.decode("utf-8")


def token_count(node):
    n, stack = 0, [node]
    while stack:
        x = stack.pop()
        if x.child_count == 0:
            n += x.type != "comment"
        else:
            stack.extend(x.children)
    return n


def delete_text(stmt):
    p = stmt.parent
    return "" if p is not None and p.type in BLOCKS else ";"


def base_type(typ):
    return " ".join(_QUALIFIERS.sub(" ", typ or "").split())


SCALARS = ("char", "short", "int", "long", "signed", "unsigned", "_Bool", "bool",
           "float", "double", "size_t", "enum")


def default_value(typ, ptr=0, typedefs=None, element=False):
    if ptr:
        return "0"
    base = base_type(typ)
    if base in INT_DEFAULTS:
        return INT_DEFAULTS[base]
    resolved = base
    for _ in range(8):
        t = typedefs.get(resolved) if typedefs else None
        if t is None:
            break
        resolved = base_type(t.underlying)
    if element:
        resolved = re.sub(r"__attribute__\s*\(\(.*\)\)", "", resolved).strip()
        return default_value(resolved, 0, typedefs)
    if "__attribute__" in resolved or resolved.startswith(("struct ", "union ")):
        return f"({base}){{0}}"
    if resolved in INT_DEFAULTS:
        return INT_DEFAULTS[resolved]
    if resolved in ("float", "double", "long double"):
        return "0.0"
    if resolved and not any(w in SCALARS for w in resolved.split()):
        return f"({base}){{0}}"
    return "0"


def unwrap_declarator(node):
    ptr = arr = 0
    func = None
    n = node
    while n is not None:
        if n.type in NAME_TYPES or n.type == "field_identifier":
            return n, ptr, arr, func
        if n.type == "pointer_declarator":
            if func is not None:
                return None, ptr, arr, None
            ptr += 1
        elif n.type == "array_declarator":
            arr += 1
        elif n.type == "function_declarator" and func is None:
            func = n
        nxt = n.child_by_field_name("declarator")
        if nxt is None:
            nxt = next((c for c in n.named_children
                        if c.type != "comment"), None)
        n = nxt
    return None, ptr, arr, func


def specifier_text(decl_node):
    parts = [text(c) for c in decl_node.children if c.type in TYPE_NODES]
    return " ".join(parts) if parts else None


def comma_span(node):
    s, e = node.start_byte, node.end_byte
    prev, nxt = node.prev_sibling, node.next_sibling
    if prev is not None and prev.type == ",":
        return prev.start_byte, e
    if nxt is not None and nxt.type == ",":
        return s, nxt.end_byte
    return s, e


def list_delete(elements, selected):
    idx = sorted(selected)
    if not idx:
        return []
    if len(idx) == len(elements):
        return [(elements[0].start_byte, elements[-1].end_byte, "")]
    edits, runs = [], []
    for i in idx:
        if runs and runs[-1][1] == i - 1:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    for a, b in runs:
        if b + 1 < len(elements):
            edits.append((elements[a].start_byte, elements[b + 1].start_byte, ""))
        else:
            edits.append((elements[a - 1].end_byte, elements[b].end_byte, ""))
    return edits


PRIMARY = ("identifier", "number_literal", "char_literal", "string_literal",
           "concatenated_string", "call_expression", "parenthesized_expression",
           "subscript_expression", "field_expression", "true", "false", "null")
SAFE_CONTEXTS = ("expression_statement", "return_statement", "init_declarator",
                 "argument_list", "parenthesized_expression", "initializer_list")


def needs_parens(expr, site):
    if expr.type in PRIMARY:
        return False
    p = site.parent
    if p is None:
        return True
    if p.type in SAFE_CONTEXTS:
        return False
    if p.type == "assignment_expression":
        right = p.child_by_field_name("right")
        return right is None or right.id != site.id
    return True


def has_side_effects(node):
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in SIDE_EFFECTS:
            return True
        stack.extend(n.children)
    return False


def literal_text(node):
    n = node
    while n is not None and n.type == "parenthesized_expression":
        n = next((c for c in n.named_children if c.type != "comment"), None)
    if n is None:
        return None
    if n.type in LITERALS:
        return text(n)
    if n.type == "unary_expression" and n.child_count == 2 \
            and n.children[0].type in ("-", "+", "~") \
            and n.children[1].type in LITERALS:
        return f"({text(n)})"
    return None


def uniform_literal(init):
    if init is None:
        return None
    if init.type != "initializer_list":
        return literal_text(init)
    vals = set()
    stack = [init]
    while stack:
        n = stack.pop()
        if n.type == "initializer_list":
            stack.extend(c for c in n.named_children if c.type != "comment")
            continue
        lit = literal_text(n)
        if lit is None:
            return None
        vals.add(lit)
    return vals.pop() if len(vals) == 1 else None


def param_list(fdecl):
    params = fdecl.child_by_field_name("parameters") if fdecl else None
    if params is None:
        return [], False
    plist = [c for c in params.named_children if c.type == "parameter_declaration"]
    if len(plist) == 1 and plist[0].child_by_field_name("declarator") is None \
            and text(plist[0]).strip() == "void":
        plist = []
    variadic = any(c.type == "variadic_parameter" or c.type == "..."
                   for c in params.children)
    return plist, variadic


class Decl:
    __slots__ = ("kind", "name", "key", "spans", "typ", "ptr", "arr", "uses",
                 "users", "written", "read", "init", "fn", "index", "group",
                 "addr", "pinned", "params", "arity", "variadic",
                 "proto_params", "calls", "inline", "defn", "protos",
                 "proto_items", "underlying", "keep", "dnode",
                 "def_params", "inline_expr", "elem_init", "init_elems")

    def __init__(self, kind, name, key):
        self.kind, self.name, self.key = kind, name, key
        self.spans = []
        self.typ, self.ptr, self.arr = None, 0, 0
        self.uses = []
        self.users = set()
        self.written = self.read = self.addr = self.pinned = False
        self.variadic = False
        self.init = self.fn = self.index = self.group = None
        self.keep = self.dnode = None
        self.def_params = []
        self.inline_expr = self.elem_init = None
        self.init_elems = []
        self.params = []
        self.arity = None
        self.proto_params = []
        self.calls = []
        self.inline = self.defn = self.underlying = None
        self.protos = []
        self.proto_items = []


class CModel:

    def __init__(self, content, root=None):
        self.content = content
        self.src = content.encode("utf-8")
        if root is None:
            root = parsers.C_PARSER.parse(self.src).root_node
        self.root = root
        self.decls = {}
        self.functions, self.globals, self.typedefs = {}, {}, {}
        self.groups = {}
        self.struct_fields = {}
        self.struct_order = {}
        self.init_owner = {}
        self.order = []
        self.def_sites = set()
        self.ordinals = {}
        self._collect_top_level()
        self._resolve()
        for d in self.typedefs.values():
            if (d.arity - 1) * len(d.uses) <= d.arity + 3:
                self._add(d)
        for f in self.functions.values():
            if f.defn is not None and f.name != "main" and not f.addr \
                    and not f.variadic:
                f.inline = self._inline_info(f)

    def _add(self, d):
        self.decls[d.key] = d
        return d

    def _function(self, name):
        d = self.functions.get(name)
        if d is None:
            d = self._add(Decl("function", name,
                               DeclarationNode(name, "function", None)))
            self.functions[name] = d
        return d

    def _collect_top_level(self):
        for item in self.root.named_children:
            t = item.type
            if t == "function_definition":
                leaf, ptr, _, fdecl = unwrap_declarator(
                    item.child_by_field_name("declarator"))
                if leaf is None or fdecl is None:
                    self.order.append((item, None))
                    continue
                self.def_sites.add(leaf.id)
                f = self._function(text(leaf))
                f.defn = item
                f.typ, f.ptr = specifier_text(item), ptr
                plist, f.variadic = param_list(fdecl)
                f.arity = len(plist)
                if f.name != "main":
                    f.spans.append((item.start_byte, item.end_byte, ""))
                self.order.append((item, f))
            elif t == "declaration":
                self.order.append((item, self._top_declaration(item)))
            elif t == "type_definition":
                self.order.append((item, self._typedef(item)))
            else:
                if t in ("struct_specifier", "union_specifier"):
                    self._struct_fields(item)
                self.order.append((item, None))

    def _struct_fields(self, node):
        tag = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if tag is None or body is None:
            return
        kind = "struct" if node.type == "struct_specifier" else "union"
        fields, order = {}, []
        for fd in body.named_children:
            if fd.type != "field_declaration":
                continue
            typ = specifier_text(fd)
            dns = fd.children_by_field_name("declarator")
            for dn in dns:
                leaf, ptr, arr, _ = unwrap_declarator(dn)
                if leaf is None:
                    continue
                fname = text(leaf)
                fields[fname] = (typ, ptr, arr)
                if kind != "struct":
                    continue
                name = f"{text(tag)}:{fname}"
                d = self._add(Decl("field", name,
                                   DeclarationNode(name, "field", None)))
                d.typ, d.ptr, d.arr = typ, ptr, arr
                d.group, d.dnode = fd.id, dn
                self.groups.setdefault(fd.id, (fd, []))[1].append(d)
                order.append(d)
        self.struct_fields[f"{kind} {text(tag)}"] = fields
        if kind == "struct":
            self.struct_order[text(tag)] = order

    def _struct_tag(self, typ):
        base = base_type(typ)
        for _ in range(8):
            t = self.typedefs.get(base)
            if t is None:
                break
            base = base_type(t.underlying)
        return base[7:].strip() if base.startswith("struct ") else None

    def _expr_tag(self, node, scopes):
        n = node
        while n is not None:
            t = n.type
            if t == "identifier":
                d = self._lookup(text(n), scopes)
                return self._struct_tag(d.typ) if d is not None \
                    and d.kind != "function" else None
            if t == "parenthesized_expression":
                n = next((c for c in n.named_children if c.type != "comment"), None)
            elif t in ("subscript_expression", "pointer_expression"):
                n = n.child_by_field_name("argument")
            elif t == "field_expression":
                tag = self._expr_tag(n.child_by_field_name("argument"), scopes)
                fld = n.child_by_field_name("field")
                info = self.struct_fields.get(f"struct {tag}", {}).get(
                    text(fld)) if tag and fld is not None else None
                return self._struct_tag(info[0]) if info else None
            elif t == "call_expression":
                fn = n.child_by_field_name("function")
                f = self.functions.get(text(fn)) if fn is not None else None
                return self._struct_tag(f.typ) if f is not None else None
            else:
                return None
        return None

    def _field_use(self, n, scopes):
        fld = n.child_by_field_name("field")
        if fld is None:
            return
        fname = text(fld)
        tag = self._expr_tag(n.child_by_field_name("argument"), scopes)
        d = self.decls.get(DeclarationNode(f"{tag}:{fname}", "field", None)) \
            if tag else None
        if d is None:
            for o in self.struct_order.values():
                for x in o:
                    if x.name.endswith(f":{fname}"):
                        x.pinned = True
            return
        self._value_use(d, n)

    def _struct_init(self, typ, ptr, arr, value, dn):
        tag = self._struct_tag(typ)
        if tag is None or ptr or value is None \
                or value.type != "initializer_list":
            return
        lists = [value]
        for _ in range(arr):
            lists = [c for l in lists for c in l.named_children
                     if c.type == "initializer_list"]
        fields = self.struct_order.get(tag, [])
        for l in lists:
            elems = [c for c in l.named_children if c.type != "comment"]
            self.init_owner[id(elems)] = (tag, dn)
            for i, e in enumerate(elems):
                if e.type == "initializer_pair":
                    des = e.child_by_field_name("designator")
                    name = text(des).lstrip(".") if des is not None else None
                    target = next((f for f in fields
                                   if f.name.endswith(f":{name}")), None)
                    if target is not None:
                        target.init_elems.append((elems, i))
                elif i < len(fields):
                    fields[i].init_elems.append((elems, i))

    def _top_declaration(self, item):
        for c in item.children:
            if c.type in ("struct_specifier", "union_specifier"):
                self._struct_fields(c)
        declarators = item.children_by_field_name("declarator")
        typ = specifier_text(item)
        owner = None
        self.groups[item.id] = (item, [])
        for dn in declarators:
            target = dn.child_by_field_name("declarator") \
                if dn.type == "init_declarator" else dn
            leaf, ptr, arr, fdecl = unwrap_declarator(target)
            if leaf is None:
                continue
            self.def_sites.add(leaf.id)
            name = text(leaf)
            if fdecl is not None:
                f = self._function(name)
                if f.typ is None:
                    f.typ, f.ptr = typ, ptr
                if f.arity is None:
                    plist, f.variadic = param_list(fdecl)
                    f.arity = len(plist)
                span = (item.start_byte, item.end_byte, "") \
                    if len(declarators) == 1 else (*comma_span(dn), "")
                f.spans.append(span)
                f.protos.append(fdecl)
                f.proto_items.append(item)
                owner = owner or f
                continue
            g = self.globals.get(name)
            if g is None:
                g = self._add(Decl("global_variable", name, DeclarationNode(
                    name, "global_variable", None)))
                self.globals[name] = g
            else:
                g.pinned = True
            g.typ, g.ptr, g.arr = typ, ptr, arr
            g.group, g.dnode = item.id, dn
            self.groups[item.id][1].append(g)
            if dn.type == "init_declarator":
                self._set_init(g, dn.child_by_field_name("value"))
                self._struct_init(typ, ptr, arr, dn.child_by_field_name("value"), dn)
            owner = owner or g
        return owner

    def _set_init(self, d, value):
        if value is None:
            return
        if value.type == "initializer_list":
            if d.arr:
                d.elem_init = uniform_literal(value)
        elif not d.arr:
            d.init = literal_text(value)

    def _typedef(self, item):
        declarators = item.children_by_field_name("declarator")
        if len(declarators) != 1 or declarators[0].type not in NAME_TYPES:
            return None
        tnode = item.child_by_field_name("type")
        if tnode is None or tnode.child_by_field_name("body") is not None:
            return None
        name = text(declarators[0])
        self.def_sites.add(declarators[0].id)
        under = " ".join(text(c) for c in item.children
                         if c.type not in ("typedef", ";", "comment")
                         and c.id != declarators[0].id)
        d = Decl("typedef", name, DeclarationNode(name, "typedef", None))
        d.arity = token_count(item) - 3
        d.underlying = under
        d.spans.append((item.start_byte, item.end_byte, ""))
        d.defn = item
        self.typedefs[name] = d
        return d

    def _resolve(self):
        for item, owner in self.order:
            if item.type == "function_definition" and owner is not None:
                self._walk_function(item, owner)
                continue
            if item.type == "declaration" and owner is not None \
                    and owner.kind == "function":
                for fdecl, pitem in zip(owner.protos, owner.proto_items):
                    if pitem.id == item.id:
                        self._proto_params(owner, fdecl)
            self._walk(item, [{}], owner)

    def _proto_params(self, f, fdecl):
        plist, _ = param_list(fdecl)
        f.proto_params.append(plist)
        for pd in plist:
            leaf, _, _, _ = unwrap_declarator(pd.child_by_field_name("declarator"))
            if leaf is None:
                leaf = self._knr_name(pd)
            if leaf is not None:
                self.def_sites.add(leaf.id)

    def _walk_function(self, defn, f):
        dnode = defn.child_by_field_name("declarator")
        _, _, _, fdecl = unwrap_declarator(dnode)
        plist, _ = param_list(fdecl)
        scope = {}
        f.params = []
        f.def_params = plist
        for i, pd in enumerate(plist):
            leaf, ptr, arr, _ = unwrap_declarator(pd.child_by_field_name("declarator"))
            typ = specifier_text(pd)
            if leaf is None:
                leaf = self._knr_name(pd)
                typ = None
            if leaf is None:
                continue
            pname = text(leaf)
            self.def_sites.add(leaf.id)
            p = Decl("parameter", f"{f.name}:{pname}",
                     DeclarationNode(f"{f.name}:{pname}", "parameter", None))
            p.typ, p.ptr, p.arr = typ, ptr, arr
            p.fn, p.index, p.group = f, i, pd
            scope[pname] = p
            if f.name != "main" and not f.variadic:
                self._add(p)
                f.params.append(p)
        for c in defn.children:
            if c.id == dnode.id:
                self._walk(c, [{}], f)
            elif c.type == "compound_statement":
                self._walk(c, [{}, scope], f, fn=f)
            else:
                self._walk(c, [{}], f)

    def _knr_name(self, pd):
        t = pd.child_by_field_name("type")
        if t is not None and t.type == "type_identifier" and len(pd.named_children) == 1 \
                and text(t) not in self.typedefs:
            return t
        return None

    def _local(self, dn, decl_node, typ, f):
        target = dn.child_by_field_name("declarator") \
            if dn.type == "init_declarator" else dn
        leaf, ptr, arr, fdecl = unwrap_declarator(target)
        if leaf is None:
            return None, None
        name = text(leaf)
        self.def_sites.add(leaf.id)
        if fdecl is not None:
            return name, self._function(name)
        k = (f.name, name)
        occ = self.ordinals.get(k, 0)
        self.ordinals[k] = occ + 1
        key = DeclarationNode(f"{f.name}:{name}#{occ}", "local_variable", None)
        d = self._add(Decl("local_variable", name, key))
        d.typ, d.ptr, d.arr, d.fn = typ, ptr, arr, f
        d.group, d.dnode = decl_node.id, dn
        self.groups.setdefault(decl_node.id, (decl_node, []))[1].append(d)
        if dn.type == "init_declarator":
            value = dn.child_by_field_name("value")
            self._set_init(d, value)
            if value is not None and has_side_effects(value):
                d.keep = value
        return name, d

    def _walk(self, node, scopes, owner, fn=None):
        stack = [(node, scopes)]
        while stack:
            n, sc = stack.pop()
            t = n.type
            if t == "declaration" and fn is not None:
                typ = specifier_text(n)
                for c in n.children:
                    if c.type in TYPE_NODES:
                        self._walk(c, sc, owner, fn)
                for dn in n.children_by_field_name("declarator"):
                    name, d = self._local(dn, n, typ, fn)
                    if name is not None:
                        sc[-1][name] = d
                    if d is not None and d.kind == "local_variable" \
                            and dn.type == "init_declarator":
                        self._struct_init(typ, d.ptr, d.arr,
                                          dn.child_by_field_name("value"), dn)
                    for c in dn.children if dn.type == "init_declarator" else [dn]:
                        self._walk(c, sc, owner, fn)
                continue
            if t in ("compound_statement", "for_statement") and n.id != node.id:
                inner = sc + [{}]
                stack.extend((c, inner) for c in reversed(n.children))
                continue
            if t == "identifier":
                if n.id not in self.def_sites:
                    self._use(n, sc, owner)
                continue
            if t in ("type_identifier", "primitive_type"):
                if n.id not in self.def_sites:
                    self._type_use(n, owner)
                continue
            if t == "field_expression":
                self._field_use(n, sc)
            if t in ("field_identifier", "statement_identifier",
                     "string_literal", "comment"):
                continue
            stack.extend((c, sc) for c in reversed(n.children))

    def _note_user(self, d, owner):
        if owner is None:
            d.pinned = True
        elif owner is not d:
            d.users.add(owner)

    def _type_use(self, n, owner):
        d = self.typedefs.get(text(n))
        if d is None:
            return
        p = n.parent
        if p is not None and p.type in ("struct_specifier", "union_specifier",
                                        "enum_specifier"):
            return
        d.uses.append((n.start_byte, n.end_byte, None, True))
        self._note_user(d, owner)

    def _lookup(self, name, scopes):
        for s in reversed(scopes):
            if name in s:
                return s[name]
        return self.functions.get(name) or self.globals.get(name)

    def _use(self, n, scopes, owner):
        d = self._lookup(text(n), scopes)
        if d is None:
            if text(n) in self.typedefs:
                self._type_use(n, owner)
            return
        if d.kind in ("function", "global_variable"):
            self._note_user(d, owner)
        if d.kind == "function":
            self._function_use(d, n)
        else:
            self._value_use(d, n)

    def _function_use(self, f, n):
        callee, p = n, n.parent
        while p is not None and p.type == "parenthesized_expression":
            callee, p = p, p.parent
        fn_child = p.child_by_field_name("function") if p is not None else None
        if p is not None and p.type == "call_expression" \
                and fn_child is not None and fn_child.id == callee.id:
            args = p.child_by_field_name("arguments")
            alist = [c for c in args.named_children if c.type != "comment"] \
                if args is not None else []
            f.calls.append((p, alist))
            stmt = p.parent
            if stmt is not None and stmt.type == "expression_statement":
                f.uses.append((stmt.start_byte, stmt.end_byte,
                               delete_text(stmt), True))
            else:
                f.uses.append((p.start_byte, p.end_byte, None, True))
            return
        f.addr = True
        f.uses.append((n.start_byte, n.end_byte, "0", True))

    def _spine(self, n):
        cur, dims, field = n, 0, None
        while True:
            p = cur.parent
            if p is None:
                break
            if p.type == "subscript_expression":
                arg = p.child_by_field_name("argument")
                if arg is None or arg.id != cur.id:
                    break
                dims += 1
            elif p.type == "field_expression":
                arg = p.child_by_field_name("argument")
                if arg is None or arg.id != cur.id:
                    break
                fld = p.child_by_field_name("field")
                field = text(fld) if fld is not None else None
                dims = 0
            elif p.type == "pointer_expression" and p.children[0].type == "*":
                dims += 1
            elif p.type == "parenthesized_expression":
                q = p.parent
                if q is None or q.type not in (
                        "subscript_expression", "field_expression",
                        "pointer_expression", "parenthesized_expression",
                        "assignment_expression", "update_expression"):
                    break
            else:
                break
            cur = p
        return cur, dims, field

    def _value_use(self, d, n):
        par = n.parent
        if par is not None and par.type == "pointer_expression" \
                and par.children[0].type == "&":
            d.addr = True
            d.uses.append((par.start_byte, par.end_byte, "0", True))
            return
        top, dims, field = self._spine(n)
        if field is not None:
            read = ("field", field)
        elif dims:
            read = ("elem", d.arr + d.ptr - dims)
        elif d.arr:
            read = "0"
        else:
            read = ("value", None)
        p = top.parent
        if p is not None and ((p.type == "assignment_expression"
                               and p.child_by_field_name("left").id == top.id)
                              or p.type == "update_expression"):
            d.written = True
            stmt = p.parent
            rhs = p.child_by_field_name("right") \
                if p.type == "assignment_expression" else None
            if stmt is not None and stmt.type == "expression_statement":
                if rhs is not None and has_side_effects(rhs):
                    d.uses.append((stmt.start_byte, stmt.end_byte,
                                   ("keep", rhs), False))
                else:
                    d.uses.append((stmt.start_byte, stmt.end_byte,
                                   delete_text(stmt), False))
            elif rhs is not None and p.children[1].type == "=":
                d.uses.append((p.start_byte, p.end_byte, ("paren", rhs), False))
            else:
                d.uses.append((p.start_byte, p.end_byte, ("written",) + (read,), False))
            return
        d.read = True
        d.uses.append((top.start_byte, top.end_byte, read, True))

    def _inline_info(self, f):
        body = f.defn.child_by_field_name("body")
        if body is None or f.ptr or len(f.params) != f.arity:
            return None
        stmts = [c for c in body.named_children if c.type != "comment"]
        if len(stmts) != 1:
            return None
        s = stmts[0]
        discarded = all(call.parent is not None
                        and call.parent.type == "expression_statement"
                        for call, _ in f.calls)
        if s.type == "return_statement" or (
                s.type == "expression_statement"
                and ((f.typ or "") == "void" or discarded)):
            expr = next((c for c in s.named_children if c.type != "comment"), None)
        else:
            return None
        if expr is None:
            return None
        names = {p.name.split(":", 1)[1]: i for i, p in enumerate(f.params)}
        holes, stack = [], [expr]
        while stack:
            n = stack.pop()
            if n.type == "identifier":
                nm = text(n)
                if nm == f.name:
                    return None
                if nm in names:
                    top, _, _ = self._spine(n)
                    par = top.parent
                    if par is not None and (par.type == "update_expression" or (
                            par.type == "assignment_expression"
                            and par.child_by_field_name("left").id == top.id)):
                        return None
                    holes.append((n.start_byte, n.end_byte, names[nm]))
            stack.extend(n.children)
        f.inline_expr = expr
        body = token_count(expr) - len(holes)
        delta = 0
        for call, args in f.calls:
            if len(args) != f.arity:
                continue
            delta += body - token_count(call) + 2 * needs_parens(expr, call)
            for _, _, i in holes:
                delta += token_count(args[i]) + 2 * (args[i].type not in PRIMARY)
        if delta >= token_count(f.defn) + sum(token_count(p) for p in f.proto_items):
            return None
        return sorted(holes)

    def is_dead(self, d):
        if d.kind == "function":
            return d.name != "main" and not d.pinned and not (d.users - {d})
        if d.kind in ("global_variable", "local_variable", "field"):
            return not d.read and not d.addr and not d.pinned
        if d.kind == "parameter":
            return not d.uses and not d.fn.addr
        if d.kind == "typedef":
            return not d.uses
        return False

    def dead_closure(self):
        dead = {k for k, d in self.decls.items() if self.is_dead(d)}
        changed = True
        while changed:
            changed = False
            for k, d in self.decls.items():
                if k in dead or d.pinned or d.addr or d.kind not in (
                        "function", "global_variable", "typedef"):
                    continue
                if d.kind == "function" and d.name == "main":
                    continue
                users = d.users - {d}
                if users and all(u.key in dead for u in users):
                    dead.add(k)
                    changed = True
        return dead

    def graph(self):
        g = nx.DiGraph()
        dead = self.dead_closure()
        for key in self.decls:
            g.add_node(key, dead=key in dead)
        for key, d in self.decls.items():
            for u in d.users:
                g.add_edge(u.key, key, label="ref")
            if d.fn is not None:
                g.add_edge(d.fn.key, key, label="contains")
        for d, v in self.dominator_edges():
            if g.has_edge(d, v):
                g[d][v]["dominates"] = True
            else:
                g.add_edge(d, v, label="dominates", dominates=True)
        for key in self.prototype_keys() + self.inline_keys():
            g.add_node(key, dead=False)
        g.add_nodes_from(fragments(self.root))
        g.graph["c_model"] = self
        return g

    def dominator_edges(self):
        root = DeclarationNode("", "root", None)
        calls = nx.DiGraph()
        calls.add_node(root)
        for f in self.functions.values():
            if f.defn is None:
                continue
            calls.add_node(f.key)
            if f.name == "main" or f.pinned:
                calls.add_edge(root, f.key)
            for u in f.users:
                if u.kind == "function" and u.defn is not None:
                    calls.add_edge(u.key, f.key)
                else:
                    calls.add_edge(root, f.key)
        idom = nx.immediate_dominators(calls, root)
        return [(d, v) for v, d in idom.items() if v != root and d != root]

    def prototype_keys(self):
        return [DeclarationNode(f.name, "prototype", None)
                for f in self.functions.values()
                if f.defn is not None and f.protos and f.name != "main"]

    def inline_keys(self):
        return [DeclarationNode(f.name, "inline", None)
                for f in self.functions.values() if f.inline is not None]

    def _value_text(self, d, repl):
        if isinstance(repl, str):
            return repl
        if repl[0] == "written":
            r = repl[1]
            if isinstance(r, str):
                return r
            if r[0] == "value":
                return default_value(d.typ, d.ptr, self.typedefs)
            if r[0] == "elem" and r[1] <= 0:
                return default_value(d.typ, 0, self.typedefs, element=r[1] < 0)
            return self._value_text(d, r)
        tag, arg = repl
        if tag == "keep":
            return ("tmpl", [(arg.start_byte, arg.end_byte), ";"])
        if tag == "paren":
            return ("tmpl", ["(", (arg.start_byte, arg.end_byte), ")"])
        const = d.init if (d.init is not None and not d.written
                           and not d.addr) else None
        if tag == "elem":
            if arg > 0:
                return "0"
            elem = d.elem_init if not d.written and not d.addr else None
            return elem or default_value(d.typ, 0, self.typedefs, element=arg < 0)
        if tag == "field":
            info = self.struct_fields.get(base_type(d.typ), {}).get(arg)
            if info is None:
                return "0"
            typ, ptr, arr = info
            return "0" if (ptr or arr) else default_value(typ, 0, self.typedefs)
        return const or default_value(d.typ, d.ptr, self.typedefs)

    def _typedef_text(self, d, removed):
        out = d.underlying
        for _ in range(8):
            new = out
            for name, t in removed.items():
                if t is not d:
                    new = re.sub(rf"\b{re.escape(name)}\b", t.underlying, new)
            if new == out:
                break
            out = new
        return out

    def assemble(self, keys):
        keys = sorted(keys, key=lambda k: (k.node_type, k.name))
        sel = [self.decls[k] for k in keys if k in self.decls]
        protos = [self.functions[k.name] for k in keys
                  if k.node_type == "prototype" and k.name in self.functions]
        inlines = {k.name for k in keys if k.node_type == "inline"
                   and k.name in self.functions
                   and self.functions[k.name].inline is not None}
        if not sel and not protos and not inlines:
            return self.content
        edits, lists = [], {}

        def drop(elements, i):
            lists.setdefault(id(elements), (elements, set()))[1].add(i)

        typedefs = {d.name: d for d in sel if d.kind == "typedef"}
        by_group = {}
        for d in sel:
            if d.kind in ("global_variable", "local_variable", "field"):
                by_group.setdefault(d.group, []).append(d)
        for d in sel:
            if d.kind in ("global_variable", "local_variable", "field"):
                gnode, members = self.groups[d.group]
                if len(by_group[d.group]) == len(members):
                    if by_group[d.group][0] is d:
                        edits.append(self._group_edit(gnode, members))
                else:
                    drop(members, members.index(d))
                for s, e, repl, _ in d.uses:
                    edits.append((s, e, self._value_text(d, repl)))
                for elems, i in d.init_elems:
                    drop(elems, i)
            elif d.kind == "typedef":
                edits.extend(d.spans)
                under = self._typedef_text(d, typedefs)
                edits.extend((s, e, under) for s, e, _, _ in d.uses)
            elif d.kind == "parameter":
                edits.extend(self._param_edits(d, drop))
            elif d.kind == "function":
                edits.extend(d.spans)
                for s, e, repl, _ in d.uses:
                    edits.append((s, e, repl if repl is not None
                                  else default_value(d.typ, d.ptr, self.typedefs)))
        removed_fields = {}
        for d in sel:
            if d.kind == "field":
                tag = d.name.split(":", 1)[0]
                removed_fields[tag] = removed_fields.get(tag, 0) + 1
        for elements, idx in lists.values():
            owner = self.init_owner.get(id(elements))
            if owner is not None and len(idx) == len(elements):
                tag, dn = owner
                if removed_fields.get(tag, 0) < len(self.struct_order.get(tag, [])):
                    edits.append((elements[0].start_byte, elements[-1].end_byte, "0"))
                else:
                    decl = dn.child_by_field_name("declarator")
                    value = dn.child_by_field_name("value")
                    edits.append((decl.end_byte, value.end_byte, ""))
                continue
            nodes = [m.dnode if isinstance(m, Decl) else m for m in elements]
            edits.extend(list_delete(nodes, idx))
        for f in protos:
            edits.extend(self._prototype_edits(f))
        if inlines:
            edits.extend(self._inline_edits(inlines))
        return splice(self.content, edits)

    def _group_edit(self, gnode, members):
        keeps = []
        for m in members:
            if m.keep is not None:
                keeps.extend([(m.keep.start_byte, m.keep.end_byte), "; "])
        if keeps:
            return (gnode.start_byte, gnode.end_byte, ("tmpl", keeps))
        p = gnode.parent
        return (gnode.start_byte, gnode.end_byte,
                "" if p is None or p.type in BLOCKS else ";")

    def _param_edits(self, p, drop):
        f, i = p.fn, p.index
        drop(f.def_params, i)
        edits = []
        for plist in f.proto_params:
            if len(plist) == f.arity:
                drop(plist, i)
        for call, args in f.calls:
            if len(args) == f.arity:
                drop(args, i)
        if p.written:
            body = f.defn.child_by_field_name("body")
            pos = body.children[0].end_byte
            edits.append((pos, pos,
                          f" {text(p.group)} = {default_value(p.typ, p.ptr, self.typedefs)};"))
        else:
            for s, e, repl, _ in p.uses:
                edits.append((s, e, self._value_text(p, repl)))
        return edits

    def _prototype_edits(self, f):
        defn = f.defn
        edits = [sp for sp in f.spans if sp[0] != defn.start_byte]
        extra = []
        def_specs = {text(c) for c in defn.children
                     if c.type == "storage_class_specifier"}
        for item in f.proto_items:
            for c in item.children:
                if c.type == "storage_class_specifier" \
                        and text(c) not in def_specs and text(c) not in extra \
                        and text(c) != "extern":
                    extra.append(text(c))
        prefix = " ".join(extra) + " " if extra else ""
        first_use = min((s for s, _, _, _ in f.uses
                         if not defn.start_byte <= s < defn.end_byte),
                        default=None)
        if first_use is not None and first_use < defn.start_byte:
            anchor = next((item for item, _ in self.order
                           if item.start_byte <= first_use < item.end_byte), None)
            if anchor is None:
                return []
            edits.append((defn.start_byte, defn.end_byte, ""))
            edits.append((anchor.start_byte, anchor.start_byte,
                          prefix + text(defn) + "\n"))
        elif prefix:
            edits.append((defn.start_byte, defn.start_byte, prefix))
        return edits

    def _primary(self, node, inlined):
        if node.type not in PRIMARY:
            return False
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            while fn is not None and fn.type == "parenthesized_expression":
                fn = next((c for c in fn.named_children if c.type != "comment"), None)
            return fn is None or text(fn) not in inlined
        return True

    def _inline_edits(self, names):
        edits = []
        for name in sorted(names):
            f = self.functions[name]
            edits.extend(f.spans)
            body = f.inline_expr
            for call, args in f.calls:
                if len(args) != f.arity:
                    continue
                holes = []
                for hs, he, i in f.inline:
                    arg = (args[i].start_byte, args[i].end_byte)
                    holes.append((hs, he, ("tmpl", ["(", arg, ")"]
                                           if not self._primary(args[i], names)
                                           else [arg])))
                wrap = needs_parens(body, call)
                edits.append((call.start_byte, call.end_byte, ("tmpl", [
                    "(" if wrap else "",
                    ("render", body.start_byte, body.end_byte, tuple(holes)),
                    ")" if wrap else ""])))
        return edits


def fragments(root):
    """Large statements, initializer lists and expressions, and struct types."""
    out, structs = [], set()
    stack = [(root, None)]
    while stack:
        node, fn = stack.pop()
        t, parent = node.type, node.parent
        if t == "function_definition":
            leaf, _, _, fdecl = unwrap_declarator(node.child_by_field_name("declarator"))
            fn = DeclarationNode(text(leaf), "function", None) \
                if leaf is not None and fdecl is not None else None
        elif t in ("for_statement", "if_statement"):
            in_block = parent is not None and parent.type in (
                "compound_statement", "translation_unit", "declaration_list")
            out.append(_fragment(node, t, f"{t}@", fn, in_block))
        elif t == "initializer_list":
            if parent is None or parent.type != "initializer_list":
                out.append(_fragment(node, "initializer", "init@", fn))
        elif t == "struct_specifier":
            tag = next((c for c in node.children if c.type == "type_identifier"), None)
            if tag is not None and text(tag) not in structs:
                structs.add(text(tag))
                out.append(DeclarationNode(text(tag), "struct", None))
        for value in _expression_children(node):
            if value is not None and value.type in EXPRESSIONS:
                out.append(_fragment(value, "expression", "expr@", fn))
        stack.extend((c, fn) for c in reversed(node.children))
    return [n for n in out if n is not None]


def _fragment(node, kind, prefix, fn, *extra):
    s, e = node.start_byte, node.end_byte
    if e - s < FRAGMENT_MIN_BYTES:
        return None
    return DeclarationNode(f"{prefix}{s}:{e}", kind, fn, (s, e, *extra))


def _expression_children(node):
    t = node.type
    if t == "assignment_expression":
        return [node.child_by_field_name("right")] \
            if any(c.type == "=" for c in node.children) else []
    if t == "init_declarator":
        return [node.child_by_field_name("value")]
    if t == "return_statement":
        return [next((c for c in node.children
                      if c.type not in ("return", ";", "comment")), None)]
    if t == "call_expression":
        args = node.child_by_field_name("arguments")
        return args.children if args is not None else []
    return []


def _edit_key(item):
    (s, e), _ = item
    return (s, s != e, -e)


def splice(content, edits):
    edits = list(edits)
    if not edits:
        return content
    uniq = {}
    for s, e, repl in edits:
        if s == e and (s, e) in uniq and isinstance(repl, str) \
                and isinstance(uniq[(s, e)], str) and repl not in uniq[(s, e)]:
            uniq[(s, e)] += repl
        else:
            uniq.setdefault((s, e), repl)
    items = sorted(uniq.items(), key=_edit_key)
    starts = [k[0] for k, _ in items]
    enc = content.encode("utf-8")

    def render(lo, hi, extra=(), depth=0):
        local = items[bisect.bisect_left(starts, lo):bisect.bisect_left(starts, hi)]
        inner = [((s, e), r) for s, e, r in extra if lo <= s and e <= hi]
        if inner:
            local = sorted(inner + local, key=_edit_key)
        out, prev = [], lo
        for (s, e), repl in local:
            if s < prev or e > hi:
                continue
            out.append(enc[prev:s])
            if isinstance(repl, tuple):
                for part in repl[1]:
                    if isinstance(part, str):
                        out.append(part.encode("utf-8"))
                    elif depth >= MAX_RENDER_DEPTH:
                        lo2, hi2 = part[1:3] if part[0] == "render" else part
                        out.append(enc[lo2:hi2])
                    elif part[0] == "render":
                        _, bs, be, holes = part
                        outer = tuple(x for x in extra
                                      if not (bs <= x[0] and x[1] <= be))
                        out.append(render(bs, be, outer + holes, depth + 1))
                    else:
                        out.append(render(part[0], part[1], extra, depth + 1))
            elif repl:
                out.append(repl.encode("utf-8"))
            prev = e
        out.append(enc[prev:hi])
        return b"".join(out)

    return remove_empty_lines(render(0, len(enc)).decode("utf-8"))
