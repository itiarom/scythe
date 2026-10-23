import shutil
import subprocess

import pytest

from scythe.cdeps import CModel
from scythe.graph import DeclarationNode, build_graph_from_file

pytestmark = pytest.mark.skipif(shutil.which("gcc") is None,
                                reason="gcc required")

PROGRAM = """\
typedef unsigned char uint8_t;
typedef int int32_t;
typedef unsigned int uint32_t;
int printf(const char *, ...);
static int32_t g_1 = 5L, g_2[3] = {7L, 7L, 7L};
static uint32_t g_3 = 0x1;
static int32_t *g_4 = &g_1;
static int32_t func_2(int32_t p_3, uint8_t p_4, int32_t p_5);
static uint8_t safe_add(uint8_t a, uint8_t b) { return a + b; }
static void fwd(uint32_t v) { printf("%u\\n", v); }
static int32_t func_1(void) {
  int32_t l_1 = 3L, l_2 = 1L;
  uint8_t l_3[2] = {1, 1};
  for (int i = 0; i < 2; i++) l_2 = func_2(l_1, l_3[i], g_2[1]);
  g_3 = safe_add(l_2, g_1);
  return l_2;
}
static int32_t func_2(int32_t p_3, uint8_t p_4, int32_t p_5) {
  p_5 = p_5 + 1;
  return p_3 + *g_4 + p_5;
}
int main(void) {
  int print_hash_value = 0;
  func_1();
  fwd(g_3);
  return 0;
}
"""


def key(kind, name):
    return DeclarationNode(name, kind, None)


def compiles(tmp_path, source):
    path = tmp_path / "out.c"
    path.write_text(source)
    return subprocess.run(["gcc", "-w", "-fsyntax-only", str(path)],
                          capture_output=True).returncode == 0


@pytest.fixture
def model():
    return CModel(PROGRAM)


def edges(tmp_path):
    path = tmp_path / "program.c"
    path.write_text(PROGRAM)
    graph = build_graph_from_file(str(path), "c")
    return graph, {(u.name, v.name) for u, v, d in graph.edges(data=True)
                   if d["label"] == "ref"}


def test_graph_has_call_value_and_type_edges(tmp_path):
    _, refs = edges(tmp_path)
    assert ("func_1", "func_2") in refs
    assert ("main", "fwd") in refs
    assert ("func_2", "g_4") in refs
    assert ("g_4", "g_1") in refs
    assert ("func_1", "int32_t") in refs


def test_dead_declarations(tmp_path):
    graph, _ = edges(tmp_path)
    dead = {n.name for n in graph.nodes if graph.nodes[n].get("dead")}
    assert dead == {"func_2:p_4", "main:print_hash_value#0"}


def test_typedef_inlining(model, tmp_path):
    assert key("typedef", "uint8_t") not in model.decls
    out = model.assemble({key("typedef", t) for t in ("int32_t", "uint32_t")})
    assert "typedef int" not in out and "typedef unsigned int" not in out
    assert "static int func_1(void)" in out
    assert "static unsigned int g_3" in out
    assert compiles(tmp_path, out)


def test_parameter_removal_updates_calls_and_prototypes(model, tmp_path):
    out = model.assemble({key("parameter", "func_2:p_4")})
    assert "func_2(int32_t p_3, int32_t p_5);" in out
    assert "func_2(l_1, g_2[1])" in out
    assert compiles(tmp_path, out)


def test_written_parameter_becomes_local(model, tmp_path):
    out = model.assemble({key("parameter", "func_2:p_5")})
    assert "int32_t p_5 = " in out
    assert "func_2(l_1, l_3[i])" in out
    assert compiles(tmp_path, out)


def test_all_parameters_of_a_function(model, tmp_path):
    out = model.assemble({key("parameter", f"func_2:{p}")
                          for p in ("p_3", "p_4", "p_5")})
    assert "func_2()" in out
    assert compiles(tmp_path, out)


def test_inline_trivial_functions(model, tmp_path):
    out = model.assemble({key("inline", "safe_add"), key("inline", "fwd")})
    assert "safe_add" not in out and "fwd" not in out
    assert "g_3 = l_2 + g_1;" in out
    assert 'printf("%u\\n", g_3);' in out
    assert compiles(tmp_path, out)


def test_constant_propagation_of_unwritten_locals(model, tmp_path):
    out = model.assemble({DeclarationNode("func_1:l_1#0", "local_variable", None),
                          DeclarationNode("func_1:l_3#0", "local_variable", None)})
    assert "func_2(3L, 1, g_2[1])" in out
    assert compiles(tmp_path, out)


def test_removed_write_keeps_nested_rewrites(model, tmp_path):
    out = model.assemble({key("global_variable", g)
                          for g in ("g_1", "g_2", "g_3", "g_4")})
    assert "g_" not in out
    assert compiles(tmp_path, out)


def test_prototype_removal_hoists_definition(model, tmp_path):
    out = model.assemble({key("prototype", "func_2")})
    assert out.count("func_2(int32_t p_3") == 1
    assert out.index("static int32_t func_2(") < out.index("static int32_t func_1(")
    assert compiles(tmp_path, out)


def test_partial_declarator_removal(model, tmp_path):
    out = model.assemble({DeclarationNode("func_1:l_1#0", "local_variable", None)})
    assert "int32_t l_2 = 1L;" in out
    out = model.assemble({key("global_variable", "g_1"),
                          key("global_variable", "g_2")})
    assert "static int32_t g_1" not in out and "g_2[3]" not in out
    assert compiles(tmp_path, out)


CHECKSUM = """\
unsigned ctx;
void crc32_byte(b) { ctx = b; }
void crc32_8bytes(val) { crc32_byte(val); }
void transparent_crc(unsigned long val, char *vname, int flag) { crc32_8bytes(val); }
int main(void) { int print_hash_value = 0; transparent_crc(5, "g", print_hash_value); return ctx; }
"""


def test_knr_parameters_and_nested_inlining(tmp_path):
    m = CModel(CHECKSUM)
    assert key("parameter", "crc32_byte:b") in m.decls
    out = m.assemble(set(m.inline_keys()))
    assert "crc32" not in out and "transparent_crc" not in out
    assert "ctx = 5;" in out
    assert compiles(tmp_path, out)


def test_unused_parameters_drop_call_arguments(tmp_path):
    m = CModel(CHECKSUM)
    out = m.assemble({key("parameter", "transparent_crc:vname"),
                      key("parameter", "transparent_crc:flag")})
    assert "transparent_crc(unsigned long val)" in out
    assert "transparent_crc(5);" in out
    assert compiles(tmp_path, out)


STRUCTS = """\
struct S0 { signed f2 : 18; unsigned f3; unsigned f4; };
struct S0 g_210;
struct S0 g_arr[2] = {{1, 2, 3}, {4, 5, 6}};
int func_2(void) {
  struct S0 l_1 = {24, 6738, 165};
  struct S0 *p = &l_1;
  l_1.f2 = g_210.f3;
  return p->f2 + g_arr[1].f3 + l_1.f4;
}
int main(void) { return func_2(); }
"""


def test_field_removal_rewrites_uses_and_initializers(tmp_path):
    m = CModel(STRUCTS)
    out = m.assemble({key("field", "S0:f3")})
    assert "unsigned f3" not in out
    assert "{{1, 3}, {4, 6}}" in out
    assert "l_1 = {24, 165}" in out
    assert ".f3" not in out
    assert compiles(tmp_path, out)


def test_deeply_nested_inlining(tmp_path):
    depth = 60
    call = "1"
    for _ in range(depth):
        call = f"safe_mul({call}, 2)"
    src = ("static unsigned safe_mul(unsigned a, unsigned b) { return a * b; }\n"
           f"unsigned g;\nint main(void) {{ g = {call}; return g; }}\n")
    out = CModel(src).assemble({key("inline", "safe_mul")})
    assert "safe_mul" not in out and "()" not in out.replace("main(void)", "")
    assert out.count("* 2") == depth
    assert compiles(tmp_path, out)


def test_inlining_preserves_precedence(tmp_path):
    src = ("static int safe_sub(int a, int b) { return a - b; }\n"
           "int g;\nint main(void) { g = safe_sub(1, safe_sub(5, 3)) * 2; return g; }\n")
    out = CModel(src).assemble({key("inline", "safe_sub")})
    assert "g = (1 - (5 - 3)) * 2;" in out
    assert compiles(tmp_path, out)


def test_field_removal_never_leaves_empty_initializer(tmp_path):
    src = ("struct S0 { int f0; int f4; };\n"
           "static struct S0 g_20 = {1};\n"
           "static struct S0 g_arr[2] = {{1}, {2}};\n"
           "int main(void) { return g_20.f4 + g_arr[1].f4; }\n")
    m = CModel(src)
    out = m.assemble({key("field", "S0:f0")})
    assert "{}" not in out
    assert "g_20 = {0};" in out and "g_arr[2] = {{0}, {0}};" in out
    assert compiles(tmp_path, out)
    out = m.assemble({key("field", "S0:f0"), key("field", "S0:f4")})
    assert "{}" not in out.replace("struct S0 {", "")
    assert "static struct S0 g_20;" in out and "static struct S0 g_arr[2];" in out
    assert compiles(tmp_path, out)
