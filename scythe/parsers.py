import warnings
from abc import ABC, abstractmethod

from tree_sitter import Language, Parser, Query, QueryCursor  # type: ignore
import tree_sitter_c  # type: ignore
import tree_sitter_java  # type: ignore
import tree_sitter_solidity  # type: ignore

from scythe import utils


def _load_language(grammar_module) -> Language:
    """Build a :class:`Language` from a grammar package's ``language()``.

    Up-to-date grammar packages (tree-sitter-c, tree-sitter-java) return a
    ``PyCapsule``.  Some, like tree-sitter-solidity 1.2.x, still return a raw
    int pointer; tree-sitter accepts it but warns it is deprecated.  Silence
    only that specific warning so it doesn't leak to users.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="int argument support is deprecated"
        )
        return Language(grammar_module.language())


class _Query:
    """A compiled tree-sitter query plus the capture/match accessors the
    reducer relies on.

    Tree-sitter moved query execution onto :class:`QueryCursor` in 0.22 and
    changed ``captures`` to return ``dict[name, list[Node]]``.  The call sites
    in :mod:`scythe.rewrites` were written against the historical
    contract (``captures`` yields ``(node, capture_name)`` pairs in document
    order), so we re-present that here on top of the current API.
    """

    __slots__ = ("_query",)

    def __init__(self, query: Query):
        self._query = query

    def captures(self, node):
        captures_by_name = QueryCursor(self._query).captures(node)
        pairs = [
            (n, name)
            for name, nodes in captures_by_name.items()
            for n in nodes
        ]
        # Reproduce the document order tree-sitter <= 0.21 returned, which the
        # "first matching capture wins" logic in modifications.py depends on.
        pairs.sort(key=lambda item: (item[0].start_byte, item[0].end_byte))
        return pairs

    def matches(self, node):
        return QueryCursor(self._query).matches(node)


class _LanguageHandle:
    """A tree-sitter :class:`Language` with a cached query factory.

    Compiling a query is comparatively expensive, and the reduction loops
    rebuild the same queries on every pass, so compiled queries are cached and
    reused (``Query`` objects are stateless; the per-run state lives on the
    short-lived :class:`QueryCursor`).
    """

    def __init__(self, language: Language):
        self.language = language
        self._query_cache: dict[str, Query] = {}

    def query(self, source: str) -> _Query:
        compiled = self._query_cache.get(source)
        if compiled is None:
            compiled = Query(self.language, source)
            self._query_cache[source] = compiled
        return _Query(compiled)


SOLIDITY_LANGUAGE = _LanguageHandle(_load_language(tree_sitter_solidity))
SOLIDITY_PARSER = Parser(SOLIDITY_LANGUAGE.language)

C_LANGUAGE = _LanguageHandle(_load_language(tree_sitter_c))
C_PARSER = Parser(C_LANGUAGE.language)

JAVA_LANGUAGE = _LanguageHandle(_load_language(tree_sitter_java))
JAVA_PARSER = Parser(JAVA_LANGUAGE.language)


PARSERS = {
    "solidity": SOLIDITY_PARSER,
    "c": C_PARSER,
    "java": JAVA_PARSER,
}


class TreeTraversal(ABC):
    @abstractmethod
    def get_node_visitor(self, node):
        pass

    @abstractmethod
    def get_node_exit(self, node):
        pass

    def traverse_node(self, node):
        # Perform action for the node type if defined
        visitor = self.get_node_visitor(node)
        visitor(node)
        # if node_type in actions:
        #     # Pass the node to the action
        #     actions[node_type](node)

        # Recurse into children
        for child in node.children:
            self.traverse_node(child)
        exit_node = self.get_node_exit(node)
        exit_node(node)


def get_parser(language: str):
    parser = PARSERS.get(language)
    if parser is None:
        raise Exception(f"Parser for language '{language}' was not found")
    return parser


def parse(file_name: str, language: str):
    parser = get_parser(language)
    file_content = utils.read_file(file_name).encode("utf-8")
    tree = parser.parse(file_content)
    return tree


def declaration_name(node):
    """Robustly extract a declaration's name via tree-sitter's ``name`` field.

    Preferred over positional ``children[1]`` indexing: it tolerates grammar
    layout changes and is the same accessor used to identify the declaration
    both when building the graph and when removing nodes.
    """
    name_node = node.child_by_field_name("name")
    if name_node is None:
        for child in node.children:
            if child.type == "identifier":
                name_node = child
                break
    return name_node.text.decode("utf-8") if name_node is not None else None


def parameter_signature(function_node):
    """Parameter-type signature of a ``function_definition``.

    Returns a tuple of parameter type texts, e.g. ``("uint256", "address")``.
    Together with the name and enclosing contract this forms a unique,
    position-independent identity for a declaration: it is invariant under the
    removal of *other* declarations, so it stays valid as the reducer mutates
    the source (unlike absolute byte offsets).
    """
    signature = []
    for child in function_node.children:
        if child.type == "parameter":
            type_node = child.child_by_field_name("type") or (
                child.children[0] if child.children else None
            )
            signature.append(
                type_node.text.decode("utf-8") if type_node is not None else ""
            )
    return tuple(signature)


def type_reference_names(declaration_node):
    """User-defined type names referenced in a declaration's type.

    Scans the declaration's ``type_name`` for user-defined types (e.g. the
    ``Token`` in ``Token public t;`` or ``mapping(address => Token)``),
    returning their identifier texts. Used to build ``uses-type`` graph edges so
    removing a struct/contract can cascade to the declarations typed by it.
    """
    names = set()
    for child in declaration_node.children:
        if child.type != "type_name":
            continue
        stack = [child]
        while stack:
            n = stack.pop()
            if n.type in ("user_defined_type", "type_identifier", "identifier"):
                names.add(n.text.decode("utf-8"))
            stack.extend(n.children)
    return names
