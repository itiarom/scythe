import os
import shutil
import subprocess

import pytest

from scythe import parsers

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCYTHE = os.path.join(REPO_ROOT, ".venv", "bin", "scythe")

PROGRAM = """\
#include <stdio.h>
int keep(void) { return 7; }
int dead_one(void) { return 1; }
int dead_two(void) { return 2; }
int main(void) { printf("%d\\n", keep()); return 0; }
"""

TEST_SH = """\
#!/bin/bash
src="${1:-program.c}"
bin="$(mktemp -u)"
gcc "$src" -o "$bin" 2>/dev/null || exit 1
out="$("$bin" 2>/dev/null)"
rm -f "$bin"
[ "$out" = "7" ] && exit 0 || exit 1
"""

pytestmark = [
    pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc required"),
    pytest.mark.skipif(not os.path.exists(SCYTHE), reason="scythe console script not installed"),
]


def _func_names(source):
    tree = parsers.get_parser("c").parse(source.encode("utf-8"))
    names, stack = set(), [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "function_definition":
            for child in node.children:
                if child.type == "function_declarator":
                    for cc in child.children:
                        if cc.type == "identifier":
                            names.add(cc.text.decode("utf-8"))
        stack.extend(node.children)
    return names


def _has_error(source):
    tree = parsers.get_parser("c").parse(source.encode("utf-8"))
    stack, bad = [tree.root_node], False
    while stack and not bad:
        node = stack.pop()
        bad = node.is_error or node.is_missing
        stack.extend(node.children)
    return bad


def test_c_reduction_removes_dead_functions(tmp_path):
    work = tmp_path / "c"
    work.mkdir()
    prog = work / "program.c"
    prog.write_text(PROGRAM)
    test_sh = work / "test.sh"
    test_sh.write_text(TEST_SH)
    test_sh.chmod(0o755)

    result = subprocess.run(
        [SCYTHE, "--source-file", "program.c", "--script", str(test_sh),
         "--language", "c"],
        cwd=str(work), capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr

    reduced = prog.read_text()

    assert not _has_error(reduced)

    names = _func_names(reduced)
    assert "dead_one" not in names
    assert "dead_two" not in names
    assert names <= {"keep", "main"}
    assert "main" in names

    holds = subprocess.run(["bash", str(test_sh), "program.c"],
                           cwd=str(work), capture_output=True, timeout=60)
    assert holds.returncode == 0


def test_c_reduction_many_functions_parallel(tmp_path):
    dead = "\n".join(f"int dead_{i}(void) {{ return {i}; }}" for i in range(12))
    program = (
        "#include <stdio.h>\n"
        "int keep(void) { return 7; }\n"
        f"{dead}\n"
        'int main(void) { printf("%d\\n", keep()); return 0; }\n'
    )
    work = tmp_path / "many"
    work.mkdir()
    (work / "program.c").write_text(program)
    test_sh = work / "test.sh"
    test_sh.write_text(TEST_SH)
    test_sh.chmod(0o755)

    result = subprocess.run(
        [SCYTHE, "--source-file", "program.c", "--script", str(test_sh),
         "--language", "c"],
        cwd=str(work), capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr

    reduced = (work / "program.c").read_text()
    assert not _has_error(reduced)
    names = _func_names(reduced)
    assert names <= {"keep", "main"} and "main" in names
    holds = subprocess.run(["bash", str(test_sh), "program.c"],
                           cwd=str(work), capture_output=True, timeout=60)
    assert holds.returncode == 0
