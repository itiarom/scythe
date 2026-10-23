import bisect

from scythe import parsers

_ID_QUERY = "[(identifier) (type_identifier)] @id"
_TAGGED = ("struct_specifier", "union_specifier", "enum_specifier")


def _declarator_name(node):
    while node is not None and node.type not in ("identifier",
                                                 "type_identifier"):
        nxt = node.child_by_field_name("declarator")
        if nxt is None:
            nxt = next((c for c in node.named_children
                        if c.type != "comment"), None)
        node = nxt
    return node.text.decode("utf-8") if node is not None else None


def _tagged_names(node):
    names = set()
    if node is None or node.type not in _TAGGED:
        return names
    body = node.child_by_field_name("body")
    tag = node.child_by_field_name("name")
    if body is None:
        return names
    if tag is not None:
        names.add(tag.text.decode("utf-8"))
    if node.type == "enum_specifier":
        for e in body.named_children:
            n = e.child_by_field_name("name")
            if n is not None:
                names.add(n.text.decode("utf-8"))
    return names


def _defined_names(entity):
    if entity.type == "function_definition":
        name = _declarator_name(entity.child_by_field_name("declarator"))
        return {name} if name else set()
    names = _tagged_names(entity)
    if entity.type in ("declaration", "type_definition"):
        names |= _tagged_names(entity.child_by_field_name("type"))
        for d in entity.children_by_field_name("declarator"):
            name = _declarator_name(d)
            if name:
                names.add(name)
    return names


def _index_definitions(entities):
    definitions, definition_names = {}, []
    for i, e in enumerate(entities):
        names = _defined_names(e)
        definition_names.append(names)
        for name in names:
            definitions.setdefault(name, []).append(i)
    return definitions, definition_names


def _symbol_hashes(src, entities, definitions):
    return {name: hash(tuple(src[entities[i].start_byte:entities[i].end_byte]
                             for i in idxs))
            for name, idxs in definitions.items()}


def _identifiers(root):
    ids = parsers.C_LANGUAGE.query(_ID_QUERY).captures(root)
    return ([n.start_byte for n, _ in ids],
            [n.text.decode("utf-8") for n, _ in ids])


def _references(entities, definitions, definition_names, id_starts,
                id_texts):
    starts = [e.start_byte for e in entities]
    references = [set() for _ in entities]
    for s, text in zip(id_starts, id_texts):
        if text not in definitions:
            continue
        i = bisect.bisect_right(starts, s) - 1
        if (i >= 0 and s < entities[i].end_byte
                and text not in definition_names[i]):
            references[i].add(text)
    return references


def _referrers(references, definition_names):
    referrers = {}
    for i, refs in enumerate(references):
        for r in refs:
            referrers.setdefault(r, set()).update(definition_names[i])
    return referrers


def _deps_sig(deps, symbol_hashes):
    return frozenset((d, symbol_hashes.get(d)) for d in deps)


def _inner_key_and_deps(n, src, definitions, id_starts, id_texts, seen):
    start, end = n.args[0], n.args[1]
    fn = n.parent.name if n.parent is not None else None
    deps = set(id_texts[bisect.bisect_left(id_starts, start):
                        bisect.bisect_left(id_starts, end)])
    deps &= definitions.keys()
    if fn in definitions:
        deps.add(fn)
    base = (n.node_type, fn, hash(src[start:end]))
    occ = seen.get(base, 0)
    seen[base] = occ + 1
    return base + (occ,), deps


def _top_level_deps(name, definitions, references, referrers):
    deps = set().union(*(references[i] for i in definitions[name]))
    deps |= referrers.get(name, set())
    deps.discard(name)
    return deps


class DeferralTracker:

    def __init__(self):
        self.enabled = True
        self.failed = {}
        self.skipped = 0
        self.skipped_this_sweep = 0
        self._sigs = {}

    def start_sweep(self, full):
        self.enabled = not full
        self.skipped_this_sweep = 0

    def begin_round(self, content, nodes):
        src = content.encode("utf-8")
        root = parsers.C_PARSER.parse(src).root_node
        entities = [c for c in root.named_children if c.type != "comment"]
        definitions, definition_names = _index_definitions(entities)
        symbol_hashes = _symbol_hashes(src, entities, definitions)
        id_starts, id_texts = _identifiers(root)
        references = _references(
            entities, definitions, definition_names, id_starts, id_texts
        )
        referrers = _referrers(references, definition_names)

        self._sigs = {}
        seen = {}
        for n in nodes:
            if isinstance(n.args, tuple) and len(n.args) >= 2:
                key, deps = _inner_key_and_deps(n, src, definitions, id_starts,
                                                id_texts, seen)
                self._sigs[n] = (key, _deps_sig(deps, symbol_hashes))
                continue
            # Parameters, locals and fields ("owner:name") follow their owner.
            owner = n.name.split(":", 1)[0] if n.name else None
            if owner in definitions:
                deps = _top_level_deps(owner, definitions, references,
                                       referrers)
                self._sigs[n] = ((n.node_type, n.name),
                                 (symbol_hashes[owner],
                                  _deps_sig(deps, symbol_hashes)))

    def should_probe(self, node):
        entry = self._sigs.get(node)
        if not self.enabled or entry is None:
            return True
        key, sig = entry
        if self.failed.get(key) != sig:
            return True
        self.skipped += 1
        self.skipped_this_sweep += 1
        return False

    def record_failure(self, node):
        entry = self._sigs.get(node)
        if entry is not None:
            key, sig = entry
            self.failed[key] = sig
