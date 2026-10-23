from dataclasses import dataclass
from typing import Callable, Optional, Tuple

C_TOP = ("function", "global_variable", "typedef")
C_FINE = ("parameter", "local_variable", "field")


def _not_main(n, graph):
    return n.name != "main"


def _not_placeholder(n, graph):
    return n.name != "__A"


def _dead(n, graph):
    return graph.nodes[n].get("dead", False)


@dataclass(frozen=True)
class Pass:
    kinds: Tuple[str, ...]
    driver: str = "probe"
    mode: Optional[str] = None
    where: Optional[Callable] = None
    fine: bool = False


@dataclass(frozen=True)
class Language:
    mode: str
    passes: Tuple[Pass, ...]
    defer: bool = False
    shrink_only: bool = False


LANGUAGES = {
    "solidity": Language(mode="removal", passes=(
        Pass(("contract",), mode="flatten"),
        Pass(("function", "modifier", "event")),
        Pass(("contract", "struct")),
        Pass(("state_var",)),
        Pass(("contract", "struct")),
        Pass(("var",)),
    )),
    "c": Language(mode="replacement", passes=(
        Pass(("function",), where=_not_main),
        Pass(C_TOP, driver="bulk", where=_dead),
        Pass(C_FINE, driver="bulk", where=_dead, fine=True),
        Pass(("global_variable",)),
        Pass(("struct",), driver="ddmin", where=_not_placeholder),
        Pass(("for_statement", "if_statement")),
        Pass(("initializer",)),
        Pass(("expression",)),
        Pass(C_TOP, driver="bulk", where=_dead),
        Pass(C_FINE, driver="bulk", where=_dead, fine=True),
        Pass(("typedef",), driver="bulk"),
        Pass(("inline",)),
        Pass(("parameter",), fine=True),
        Pass(("field",), fine=True),
        Pass(("local_variable",), fine=True),
        Pass(("prototype",), driver="bulk"),
    ), defer=True, shrink_only=True),
    "java": Language(mode="replacement", passes=(
        Pass(("class",), driver="ddmin", mode="flatten"),
        Pass(("function",), driver="ddmin"),
        Pass(("field",), driver="ddmin"),
        Pass(("class",), driver="ddmin"),
        Pass(("local_variable",), driver="ddmin"),
    )),
}
