"""Per-language AST rewrite engines.

Each supported language has its own :class:`ASTRemoval` subclass that turns a set
of declaration nodes into a candidate source string. ``AST_REMOVALS`` maps a
language name to its engine; :mod:`scythe.dd` dispatches candidate construction
through it.
"""
from scythe.rewrites.base import ASTRemoval, remove_empty_lines
from scythe.rewrites.solidity import SolidityDeclarationRemoval
from scythe.rewrites.c import CDeclarationRemoval
from scythe.rewrites.java import JavaDeclarationRemoval

AST_REMOVALS = {
    "solidity": SolidityDeclarationRemoval,
    "c": CDeclarationRemoval,
    "java": JavaDeclarationRemoval,
}

__all__ = [
    "ASTRemoval",
    "remove_empty_lines",
    "AST_REMOVALS",
    "SolidityDeclarationRemoval",
    "CDeclarationRemoval",
    "JavaDeclarationRemoval",
]
