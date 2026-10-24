from scythe import parsers
from scythe.cdeps import INT_DEFAULTS, CModel
from scythe.utils import remove_empty_lines

FRAGMENTS = ("for_statement", "if_statement", "initializer", "expression")
PLACEHOLDER_STRUCT = "__A"
FIELD_DEFAULTS = {
    **INT_DEFAULTS, "ssize_t": "0xDEADBEEF", "float": "0xDEADBEEF",
    "double": "0xDEADBEEFDEADBEEF", "long double": "0xDEADBEEFDEADBEEF",
    "bool": "false", "_Bool": "false", "void": "NULL",
}


class CDeclarationRemoval:
    """Candidates for C: declarations through the dependency model, structs
    folded into a placeholder, other fragments cut out."""

    @classmethod
    def build_candidate(cls, base_content, graph, removed, mode, model=None):
        kinds = {n.node_type for n in removed}
        if kinds <= set(FRAGMENTS):
            return remove_fragments(base_content, removed), model
        if kinds == {"struct"}:
            return remove_structs(base_content, {n.name for n in removed}), model
        if model is None or model.content != base_content:
            model = graph.graph.get("c_model") if graph is not None else None
            if model is None or model.content != base_content:
                model = CModel(base_content)
        return model.assemble(removed), model


def remove_fragments(content, nodes):
    return splice_edits(content, [_fragment_edit(n) for n in nodes])


def _fragment_edit(n):
    if n.node_type == "initializer":
        repl = "{0}"
    elif n.node_type == "expression":
        repl = "0xDEADBEEF"
    else:
        repl = "" if n.args[2] else ";"
    return (n.args[0], n.args[1], repl)


def splice_edits(content, edits):
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


def _field_default(typ):
    if typ is None:
        return "NULL"
    return FIELD_DEFAULTS.get(typ.strip(), "0")


def _find_ancestor(node, typ):
    cur = node.parent
    while cur is not None:
        if cur.type == typ:
            return cur
        cur = cur.parent
    return None


def _type_text(node):
    for c in node.children:
        if c.type in ("function_declarator", "init_declarator",
                      "array_declarator", "pointer_declarator", ";"):
            break
        if c.type in ("primitive_type", "sized_type_specifier",
                      "type_identifier", "struct_specifier",
                      "union_specifier", "enum_specifier"):
            return c.text.decode("utf-8")
    return None


def _declared_names(decl):
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


def _struct_field_types(struct_node):
    out = {}
    for fl in struct_node.children:
        if fl.type != "field_declaration_list":
            continue
        for fd in fl.children:
            if fd.type != "field_declaration":
                continue
            ftype = _type_text(fd)
            stack = list(fd.children)
            while stack:
                n = stack.pop()
                if n.type == "field_identifier":
                    out[n.text.decode("utf-8")] = ftype
                elif n.type in ("array_declarator", "pointer_declarator"):
                    stack.extend(n.children)
    return out


def _struct_type_of(decl, names):
    for c in decl.children:
        if c.type == "struct_specifier":
            ti = [x for x in c.children if x.type == "type_identifier"]
            if ti and ti[0].text.decode("utf-8") in names:
                return ti[0].text.decode("utf-8")
    return None


def _leftmost_id(node):
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


def _is_assign_target(node):
    p = node.parent
    if p is not None and p.type == "update_expression":
        return True
    asg = _find_ancestor(node, "assignment_expression")
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


def remove_structs(content, names):
    """Delete the struct definitions, retype their uses to a placeholder struct
    and replace accesses to their fields by constants."""
    names = names - {PLACEHOLDER_STRUCT}
    if not names:
        return content
    root = parsers.C_PARSER.parse(content.encode("utf-8")).root_node
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
                field_type.update(_struct_field_types(n))
                end = n.end_byte
                sib = n.next_sibling
                if sib is not None and sib.type == ";":
                    end = sib.end_byte
                def_spans.append((n.start_byte, end))
        elif n.type in ("declaration", "parameter_declaration") \
                and _struct_type_of(n, names):
            svars.update(_declared_names(n))
            for c in n.children:
                if c.type != "init_declarator":
                    continue
                decl = c.child_by_field_name("declarator")
                val = c.child_by_field_name("value")
                if decl is not None and val is not None \
                        and val.type == "initializer_list":
                    edits.append((decl.end_byte, val.end_byte, ""))
    if not def_spans:
        return content

    for ti in rename_ti:
        edits.append((ti.start_byte, ti.end_byte, PLACEHOLDER_STRUCT))
    placeholder_exists = f"struct {PLACEHOLDER_STRUCT}" in content
    for i, (s, e) in enumerate(sorted(def_spans)):
        if i == 0 and not placeholder_exists:
            edits.append((s, e, f"struct {PLACEHOLDER_STRUCT} {{}};"))
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
        if fname not in field_type or _leftmost_id(base) not in svars:
            continue
        const = _field_default(field_type[fname])
        if _is_assign_target(n):
            p = n.parent
            if p is not None and p.type == "update_expression":
                edits.append((p.start_byte, p.end_byte, const))
            else:
                asg = _find_ancestor(n, "assignment_expression")
                if asg is not None:
                    edits.append((asg.start_byte, asg.end_byte, const))
        else:
            edits.append((n.start_byte, n.end_byte, const))
    return splice_edits(content, edits)
