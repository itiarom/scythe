"""Tests for Solidity declaration removal.

Each removal must keep the program **semantically valid** -- removing a
declaration also removes its references (inheritance, modifier usages, event
emits, library `using`s, calls) so the result has no reference errors. Where the
required solc version is available we assert the reduced program still compiles;
otherwise we fall back to a syntactic (no parse error) check.
"""
import subprocess

import pytest

from reducer import parsers
from reducer.graph import build_graph_from_file
from reducer.modifications import AST_REMOVALS

SOLC_VERSION = "0.5.0"

# A small, self-contained program exercising every reference kind:
#  - `onlyOwner` modifier defined in Base, used in Base.ping and Token.setTotal
#  - `Logged` event defined in Base, emitted in both contracts
#  - `Token is Base` inheritance
#  - `Unused` standalone contract
SNIPPET = """pragma solidity 0.5.0;

contract Base {
    address public owner;
    event Logged(uint256 value);
    modifier onlyOwner() { require(msg.sender == owner); _; }
    constructor() public { owner = msg.sender; }
    function ping() public onlyOwner { emit Logged(1); }
}

contract Token is Base {
    uint256 public total;
    function setTotal(uint256 v) public onlyOwner { total = v; emit Logged(v); }
}

contract Unused {
    function noop() public pure returns (uint256) { return 0; }
}
"""


def _solc_available():
    try:
        return subprocess.run(
            ["solc-select", "use", SOLC_VERSION], capture_output=True
        ).returncode == 0
    except FileNotFoundError:
        return False


SOLC_OK = _solc_available()


def _parses_clean(code):
    return not parsers.PARSERS["solidity"].parse(code.encode("utf-8")).root_node.has_error


def _compiles(code, tmp_path):
    f = tmp_path / "out.sol"
    f.write_text(code)
    result = subprocess.run(["solc", str(f)], capture_output=True, text=True)
    return result.returncode == 0, result.stderr


@pytest.fixture
def graph_and_src(tmp_path):
    f = tmp_path / "snippet.sol"
    f.write_text(SNIPPET)
    return build_graph_from_file(str(f), "solidity"), SNIPPET


def _remove(graph, src, node_type, name):
    nodes = {n for n in graph.nodes
             if n.node_type == node_type and n.name == name}
    assert nodes, f"no {node_type} named {name!r} found in graph"
    return AST_REMOVALS["solidity"](src, graph).remove_nodes(nodes, "removal")


@pytest.mark.skipif(not SOLC_OK, reason=f"solc {SOLC_VERSION} not installed")
def test_snippet_baseline_compiles(tmp_path):
    ok, stderr = _compiles(SNIPPET, tmp_path)
    assert ok, f"baseline snippet should compile:\n{stderr}"


@pytest.mark.parametrize("node_type,name,declaration", [
    ("modifier", "onlyOwner", "modifier onlyOwner"),
    ("event", "Logged", "event Logged"),
    ("contract", "Unused", "contract Unused"),
    ("contract", "Base", "contract Base"),
])
def test_removal_keeps_program_valid(graph_and_src, tmp_path, node_type, name, declaration):
    graph, src = graph_and_src
    out = _remove(graph, src, node_type, name)
    # the declaration itself is gone
    assert declaration not in out
    # result is syntactically valid
    assert _parses_clean(out)
    # and (when solc is available) free of reference errors
    if SOLC_OK:
        ok, stderr = _compiles(out, tmp_path)
        assert ok, f"removing {node_type} {name} left a reference error:\n{stderr}"


def test_modifier_usages_are_removed(graph_and_src):
    """Removing a modifier also strips every `... onlyOwner ...` usage."""
    graph, src = graph_and_src
    out = _remove(graph, src, "modifier", "onlyOwner")
    assert "onlyOwner" not in out


def test_event_emits_are_removed(graph_and_src):
    """Removing an event also strips every `emit Logged(...)` statement."""
    graph, src = graph_and_src
    out = _remove(graph, src, "event", "Logged")
    assert "Logged" not in out


def test_base_contract_removal_cascades(graph_and_src):
    """Removing a base contract cleans inheritance and inherited-member uses,
    while the dependent contract survives."""
    graph, src = graph_and_src
    out = _remove(graph, src, "contract", "Base")
    assert "contract Base" not in out
    assert "is Base" not in out      # inheritance reference cleaned
    assert "onlyOwner" not in out    # inherited modifier usage cleaned
    assert "Logged" not in out       # inherited event emits cleaned
    assert "contract Token" in out   # dependent contract is kept


def test_unselected_declarations_are_kept(graph_and_src):
    """Only the selected declaration (and its references) is removed."""
    graph, src = graph_and_src
    out = _remove(graph, src, "modifier", "onlyOwner")
    assert "event Logged" in out
    assert "contract Token" in out
    assert "contract Unused" in out


# --- struct / state variable / local variable removal + use-site cleanup ------

# `Pair` is used only in a local; `counter` only in `bump`; `tmp` is an unused
# local -- so each can be removed cleanly (declaration + every use) and the
# remaining program must still compile.
GAPS_SNIPPET = """pragma solidity 0.5.0;

contract C {
    struct Pair { uint256 a; uint256 b; }
    uint256 public counter;
    uint256 public total;

    function bump() public { counter = counter + 1; }
    function addTotal(uint256 v) public { total = total + v; }
    function f(uint256 v) public pure returns (uint256) {
        uint256 tmp = v + 1;
        Pair memory p = Pair(1, 2);
        return v;
    }
}
"""


@pytest.fixture
def gaps_graph_and_src(tmp_path):
    f = tmp_path / "gaps.sol"
    f.write_text(GAPS_SNIPPET)
    return build_graph_from_file(str(f), "solidity"), GAPS_SNIPPET


def test_graph_emits_struct_statevar_var(gaps_graph_and_src):
    """The graph must surface struct / state_var / var so they are candidates."""
    graph, _ = gaps_graph_and_src
    by_type = {}
    for n in graph.nodes:
        by_type.setdefault(n.node_type, set()).add(n.name)
    assert "Pair" in by_type.get("struct", set())
    assert {"counter", "total"} <= by_type.get("state_var", set())
    assert {"tmp", "p"} <= by_type.get("var", set())


@pytest.mark.skipif(not SOLC_OK, reason=f"solc {SOLC_VERSION} not installed")
def test_gaps_snippet_baseline_compiles(tmp_path):
    ok, stderr = _compiles(GAPS_SNIPPET, tmp_path)
    assert ok, f"baseline snippet should compile:\n{stderr}"


@pytest.mark.parametrize("node_type,name", [
    ("struct", "Pair"),
    ("state_var", "counter"),
    ("var", "tmp"),
])
def test_struct_statevar_var_removal_is_valid(gaps_graph_and_src, tmp_path, node_type, name):
    graph, src = gaps_graph_and_src
    out = _remove(graph, src, node_type, name)
    # declaration and all references to it are gone
    assert name not in out
    assert _parses_clean(out)
    if SOLC_OK:
        ok, stderr = _compiles(out, tmp_path)
        assert ok, f"removing {node_type} {name} left a reference error:\n{stderr}"


def test_state_var_use_sites_removed(gaps_graph_and_src):
    """Removing a state var strips its declaration *and* its uses."""
    graph, src = gaps_graph_and_src
    out = _remove(graph, src, "state_var", "counter")
    assert "counter" not in out          # declaration + `counter = counter + 1;`
    assert "total" in out                # the other state var is untouched
    assert "function bump" in out        # its function survives (now empty)


# --- type-use cascade (option B): removing a type drags its typed decls along --

# `Pair`/`Helper` are used as the *types* of state vars, which are in turn used in
# functions. Removing the type must cascade to those state vars and their uses.
CASCADE_SNIPPET = """pragma solidity 0.5.0;

contract Helper {
    function ping() public pure returns (uint256) { return 1; }
}

contract Main {
    struct Pair { uint256 a; uint256 b; }
    Pair internal origin;
    Helper internal helper;

    function getA() public view returns (uint256) { return origin.a; }
    function useHelper() public view returns (uint256) { return helper.ping(); }
    function keep() public pure returns (uint256) { return 42; }
}
"""


@pytest.fixture
def cascade_graph_and_src(tmp_path):
    f = tmp_path / "cascade.sol"
    f.write_text(CASCADE_SNIPPET)
    return build_graph_from_file(str(f), "solidity"), CASCADE_SNIPPET


def test_uses_type_edges_built(cascade_graph_and_src):
    graph, _ = cascade_graph_and_src
    uses = {(u.name, v.name) for u, v, d in graph.edges(data=True)
            if d.get("label") == "uses-type"}
    assert ("Pair", "origin") in uses
    assert ("Helper", "helper") in uses


@pytest.mark.parametrize("node_type,name,typed_var", [
    ("struct", "Pair", "origin"),
    ("contract", "Helper", "helper"),
])
def test_type_use_cascade_is_valid(cascade_graph_and_src, tmp_path, node_type, name, typed_var):
    graph, src = cascade_graph_and_src
    out = _remove(graph, src, node_type, name)
    assert f"{node_type} {name}" not in out  # the type declaration is gone
    assert typed_var not in out              # the state var typed by it cascaded away
    assert _parses_clean(out)
    assert "function keep" in out            # unrelated code survives
    if SOLC_OK:
        ok, stderr = _compiles(out, tmp_path)
        assert ok, f"type-use cascade for {name} left a reference error:\n{stderr}"


# --- inheritance flattening: eliminate a base by promoting members to children -

FLATTEN_SNIPPET = """pragma solidity 0.5.0;

contract Base {
    uint256 internal shared;
    function setShared(uint256 v) internal { shared = v; }
}

contract Derived is Base {
    function use() public returns (uint256) { setShared(7); return shared; }
}
"""


@pytest.fixture
def flatten_graph_and_src(tmp_path):
    f = tmp_path / "flatten.sol"
    f.write_text(FLATTEN_SNIPPET)
    return build_graph_from_file(str(f), "solidity"), FLATTEN_SNIPPET


def _flatten(graph, src, name):
    nodes = {n for n in graph.nodes if n.node_type == "contract" and n.name == name}
    assert nodes, f"no contract {name!r} in graph"
    return AST_REMOVALS["solidity"](src, graph).flatten_inheritance(nodes)


def test_flatten_eliminates_base_and_promotes_members(flatten_graph_and_src, tmp_path):
    graph, src = flatten_graph_and_src
    out = _flatten(graph, src, "Base")
    assert "contract Base" not in out          # the base is gone
    assert "is Base" not in out                # inheritance reference rewired away
    assert "shared" in out                     # its field promoted into Derived
    assert "function setShared" in out         # its method promoted too
    assert "contract Derived" in out
    assert _parses_clean(out)
    if SOLC_OK:
        ok, stderr = _compiles(out, tmp_path)
        assert ok, f"flattened program failed to compile:\n{stderr}"


def test_flatten_leaf_contract_is_noop(flatten_graph_and_src):
    """A contract with no children has nothing to flatten."""
    graph, src = flatten_graph_and_src
    out = _flatten(graph, src, "Derived")
    assert out == src


# --- type-use placeholder retyping (fallback after deletion) -------------------

# `Point` is used as a *parameter* type (deletion cannot touch it -- removing the
# param would change `dist`'s signature) AND as a state var (deletable). `Pair`
# is used only in a local, so it is fully deletable. Removing `Point` must keep
# `dist` by retyping its param to a placeholder `struct __S` seeded with Point's
# members (so `p.x + p.y` still type-checks); removing `Pair` needs no placeholder.
PLACEHOLDER_SNIPPET = """pragma solidity 0.5.0;

contract C {
    struct Point { uint256 x; uint256 y; }
    struct Pair { uint256 a; uint256 b; }

    Point internal origin;

    function dist(Point memory p) internal pure returns (uint256) {
        return p.x + p.y;
    }
    function viaField() public view returns (uint256) { return origin.x; }
    function usesPair() public pure returns (uint256) {
        Pair memory q = Pair(1, 2);
        return q.a;
    }
    function keep() public pure returns (uint256) { return 42; }
}
"""


@pytest.fixture
def placeholder_graph_and_src(tmp_path):
    f = tmp_path / "placeholder.sol"
    f.write_text(PLACEHOLDER_SNIPPET)
    return build_graph_from_file(str(f), "solidity"), PLACEHOLDER_SNIPPET


@pytest.mark.skipif(not SOLC_OK, reason=f"solc {SOLC_VERSION} not installed")
def test_placeholder_snippet_baseline_compiles(tmp_path):
    ok, stderr = _compiles(PLACEHOLDER_SNIPPET, tmp_path)
    assert ok, f"baseline snippet should compile:\n{stderr}"


def test_struct_param_retyped_to_placeholder(placeholder_graph_and_src, tmp_path):
    """A struct still used as a parameter type is retyped to `struct __S`
    (seeded with its members) instead of blocking the struct's removal."""
    graph, src = placeholder_graph_and_src
    out = _remove(graph, src, "struct", "Point")
    assert "struct Point" not in out          # the struct declaration is gone
    assert "origin" not in out                # the deletable state var cascaded away
    assert "struct __S" in out                # placeholder injected
    assert "__S memory p" in out              # the parameter was retyped
    assert "uint256 x" in out and "uint256 y" in out  # members copied over
    assert "p.x + p.y" in out                 # member access still type-checks
    assert "function dist" in out             # the function survives
    assert "function keep" in out             # unrelated code survives
    assert _parses_clean(out)
    if SOLC_OK:
        ok, stderr = _compiles(out, tmp_path)
        assert ok, f"placeholder retyping left an error:\n{stderr}"


def test_placeholder_injected_inside_contract(placeholder_graph_and_src):
    """The placeholder lives inside the contract (file-level structs are invalid
    in <0.6 Solidity), so `struct __S` appears between `contract C {` and `}`."""
    graph, src = placeholder_graph_and_src
    out = _remove(graph, src, "struct", "Point")
    assert out.index("contract C") < out.index("struct __S")


def test_fully_deletable_struct_needs_no_placeholder(placeholder_graph_and_src, tmp_path):
    """A struct used only in deletable positions is removed outright -- no
    placeholder is introduced (retyping is strictly a fallback)."""
    graph, src = placeholder_graph_and_src
    out = _remove(graph, src, "struct", "Pair")
    assert "struct Pair" not in out
    assert "__S" not in out                   # no placeholder needed
    assert _parses_clean(out)
    if SOLC_OK:
        ok, stderr = _compiles(out, tmp_path)
        assert ok, f"removing fully-deletable struct left an error:\n{stderr}"


# --- deletion completeness: control-flow headers + transitively-dead locals ----

# `xs` is read in a `for` whose bound is a *derived local* (`n = xs.length`) and
# in an `if` condition. Removing `xs` must also drop the dead local `n`, the loop,
# and the `if` -- otherwise the loop's `i < n` / the `if (xs...)` dangle and the
# program no longer compiles. Statements not touching `xs` must survive.
CONTROLFLOW_SNIPPET = """pragma solidity 0.5.0;

contract C {
    uint256[] public xs;

    function total() public view returns (uint256) {
        uint256 n = xs.length;
        uint256 s = 0;
        for (uint256 i = 0; i < n; i++) {
            s += xs[i];
        }
        if (xs.length > 0) {
            s += 1;
        }
        return s;
    }
    function keep() public pure returns (uint256) { return 7; }
}
"""


@pytest.fixture
def controlflow_graph_and_src(tmp_path):
    f = tmp_path / "controlflow.sol"
    f.write_text(CONTROLFLOW_SNIPPET)
    return build_graph_from_file(str(f), "solidity"), CONTROLFLOW_SNIPPET


@pytest.mark.skipif(not SOLC_OK, reason=f"solc {SOLC_VERSION} not installed")
def test_controlflow_snippet_baseline_compiles(tmp_path):
    ok, stderr = _compiles(CONTROLFLOW_SNIPPET, tmp_path)
    assert ok, f"baseline snippet should compile:\n{stderr}"


def test_state_var_removal_cleans_loops_and_dead_locals(controlflow_graph_and_src, tmp_path):
    graph, src = controlflow_graph_and_src
    out = _remove(graph, src, "state_var", "xs")
    assert "xs" not in out                # declaration + every use gone
    assert "for (" not in out             # the loop (bound by a dead local) is gone
    assert "n = " not in out              # the transitively-dead local is gone
    assert "if (" not in out              # the `if (xs...)` header is gone
    assert "uint256 s = 0" in out         # untouched statements survive
    assert "return s" in out
    assert "function keep" in out
    assert _parses_clean(out)
    if SOLC_OK:
        ok, stderr = _compiles(out, tmp_path)
        assert ok, f"removing xs left a dangling control-flow reference:\n{stderr}"
