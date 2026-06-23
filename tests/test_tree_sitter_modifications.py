import networkx as nx
import os
import pytest
import subprocess

from scythe import utils
from scythe.graph import DeclarationNode
from scythe.rewrites import CDeclarationRemoval
from scythe.parsers import parse

REMOVAL_FUNCTION_NAME = "safe_lshift_func_int16_t_s_s"
REMOVAL_GLOBAL_VAR_NAME_1 = "g_69"
REMOVAL_GLOBAL_VAR_NAME_2 = "g_22"
REMOVAL_GLOBAL_VAR_NAME_3 = "g_465"
REMOVAL_GLOBAL_VAR_NAME_4 = "g_466"
REMOVAL_FOR_STATEMENT = "for_101"
REMOVAL_IF_STATEMENT = "if_105"
REMOCAL_STRUCT_SPECIFIER = "S0"
REMOVAL_FUNCTION_NODE_SET = {
    DeclarationNode(REMOVAL_FUNCTION_NAME, "function", None),
    DeclarationNode(REMOVAL_GLOBAL_VAR_NAME_1, "global_variable", None),
    DeclarationNode(REMOVAL_GLOBAL_VAR_NAME_2, "global_variable", None),
    DeclarationNode(REMOVAL_GLOBAL_VAR_NAME_3, "global_variable", None),
    DeclarationNode(REMOVAL_GLOBAL_VAR_NAME_4, "global_variable", None),
    DeclarationNode(REMOVAL_FOR_STATEMENT, "for_statement", None),
    DeclarationNode(REMOVAL_IF_STATEMENT, "if_statement", None),
    DeclarationNode(REMOCAL_STRUCT_SPECIFIER, "struct", None),
}
# TEST_FILE_NAME = "./C/gcc-59903/small.c"
TEST_FILE_NAME = "./tests/utils/test_c_file.c"
TEMP_TEST_FILE_NAME = "./tests/utils/temp_test_c_file.c"

TEST_SMALL_C = "./tests/utils/test_small.c"
TEST_SMALL_C_REMOVAL_FUNCTION_NAME = "safe_div_func_uint32_t_u_u"
TEST_SMALL_C_REMOVAL_FUNCTION_NODE_SET = {
    DeclarationNode(TEST_SMALL_C_REMOVAL_FUNCTION_NAME, "function", None)
}
"""
Nodes that produce syntactical errors when removed from test_small.c
 
<Node type=if_statement, start_point=(2187, 12), end_point=(2208, 13)>
<Node type=expression_statement, start_point=(2145, 16), end_point=(2145, 1191)>
<Node type=if_statement, start_point=(1656, 4), end_point=(1724, 5)>
<Node type=if_statement, start_point=(1497, 20), end_point=(1512, 21)>
<Node type=if_statement, start_point=(1323, 20), end_point=(1338, 21)>
<Node type=if_statement, start_point=(1293, 20), end_point=(1308, 21)>
<Node type=if_statement, start_point=(1125, 20), end_point=(1140, 21)>
<Node type=if_statement, start_point=(956, 20), end_point=(1345, 21)>
<Node type=function_definition, start_point=(536, 0), end_point=(546, 1)>]
"""

@pytest.fixture
def updated_tree_removal():
    content = utils.read_file(TEST_FILE_NAME)
    modifier = CDeclarationRemoval(content, nx.DiGraph())
    updated_tree_code = modifier.remove_nodes(REMOVAL_FUNCTION_NODE_SET, "removal")
    with open(TEMP_TEST_FILE_NAME, 'w') as f:
        f.write(updated_tree_code)
    updated_tree = parse(TEMP_TEST_FILE_NAME, "c")
    yield updated_tree
    if os.path.exists(TEMP_TEST_FILE_NAME):
        os.remove(TEMP_TEST_FILE_NAME)


@pytest.fixture
def updated_tree_replacement():
    content = utils.read_file(TEST_FILE_NAME)
    modifier = CDeclarationRemoval(content, nx.DiGraph())
    updated_tree_code = modifier.remove_nodes(REMOVAL_FUNCTION_NODE_SET, "replacement")
    with open(TEMP_TEST_FILE_NAME, 'w') as f:
        f.write(updated_tree_code)
    updated_tree = parse(TEMP_TEST_FILE_NAME, "c")
    yield updated_tree
    if os.path.exists(TEMP_TEST_FILE_NAME):
        os.remove(TEMP_TEST_FILE_NAME)


@pytest.fixture
def updated_tree_combination():
    content = utils.read_file(TEST_FILE_NAME)
    modifier = CDeclarationRemoval(content, nx.DiGraph())
    updated_tree_code = modifier.remove_nodes(REMOVAL_FUNCTION_NODE_SET, "combination")
    with open(TEMP_TEST_FILE_NAME, 'w') as f:
        f.write(updated_tree_code)
    updated_tree = parse(TEMP_TEST_FILE_NAME, "c")
    yield updated_tree
    if os.path.exists(TEMP_TEST_FILE_NAME):
        os.remove(TEMP_TEST_FILE_NAME)


@pytest.fixture
def initial_tree():
    initial_tree = parse(TEST_FILE_NAME,"c")
    return initial_tree


@pytest.fixture
def small_c_tree_removal():
    # small_c_tree = parse(TEST_SMALL_C, "c")
    content = utils.read_file(TEST_SMALL_C)
    modifier = CDeclarationRemoval(content, nx.DiGraph())
    modifier.remove_nodes(
        TEST_SMALL_C_REMOVAL_FUNCTION_NODE_SET, "removal"
    )
    return
    # return updated_tree_code
    # return small_c_tree


@pytest.fixture
def small_c_tree_replacement():
    # small_c_tree = parse(TEST_SMALL_C, "c")
    content = utils.read_file(TEST_SMALL_C)
    modifier = CDeclarationRemoval(content, nx.DiGraph())
    modifier.remove_nodes(
        TEST_SMALL_C_REMOVAL_FUNCTION_NODE_SET, "replacement"
    )
    return
    # return updated_tree_code
    # return small_c_tree


@pytest.fixture
def small_c_tree_combination():
    # small_c_tree = parse(TEST_SMALL_C, "c")
    content = utils.read_file(TEST_SMALL_C)
    modifier = CDeclarationRemoval(content, nx.DiGraph())
    modifier.remove_nodes(
        TEST_SMALL_C_REMOVAL_FUNCTION_NODE_SET, "combination"
    )
    return
    # return updated_tree_code
    # return small_c_tree


def find_nodes_of_type(root_node, type):
    nodes_of_type = []
    for child in root_node.children:
        if child.type == type:
            nodes_of_type.append(child)
        else:
            nodes_of_type.extend(find_nodes_of_type(child, type))

    nodes_of_type = list(set(nodes_of_type))
    return nodes_of_type


@pytest.mark.parametrize(
    'updated_tree_fixture_name',['updated_tree_replacement', 'updated_tree_combination']  # Removal mode does not have the ability to handle return statements
)
def test_c_program_validity_after_removal(updated_tree_fixture_name, request):
    updated_tree = request.getfixturevalue(updated_tree_fixture_name)
    initial_result = subprocess.run(["gcc", TEST_FILE_NAME],
                            capture_output=True,
                            text=True)
    result = subprocess.run(["gcc", TEMP_TEST_FILE_NAME],
                            capture_output=True,
                            text=True)
    assert len(initial_result.stderr) == 0
    assert len(result.stdout) == 0
    assert len(result.stderr) == 0


@pytest.mark.parametrize(
    'updated_tree_fixture_name',['updated_tree_removal', 'updated_tree_replacement', 'updated_tree_combination']
)
def test_c_function_definition_removal(updated_tree_fixture_name, request):
    updated_tree = request.getfixturevalue(updated_tree_fixture_name)
    function_definition_nodes = find_nodes_of_type(
        updated_tree.root_node, "function_definition"
    )
    for node in function_definition_nodes:
        if node.type == "function_declarator":
            for child in node.children:
                if child.type == "identifier":
                    assert child.text.decode("utf-8") != REMOVAL_FUNCTION_NAME
                elif child.type == "parenthesized_declarator":
                    for child_child in child.children:
                        if child_child.type == "identifier":
                            assert child_child.text.decode("utf-8") != REMOVAL_FUNCTION_NAME


@pytest.mark.parametrize(
    'updated_tree_fixture_name',['updated_tree_removal', 'updated_tree_replacement', 'updated_tree_combination']
)
def test_c_expression_statement_removal(initial_tree, updated_tree_fixture_name, request):
    updated_tree = request.getfixturevalue(updated_tree_fixture_name)
    initial_expression_statement_nodes = find_nodes_of_type(
        initial_tree.root_node, "expression_statement"
    )
    updated_expression_statement_nodes = find_nodes_of_type(
        updated_tree.root_node, "expression_statement"
    )
    assert len(initial_expression_statement_nodes) > len(updated_expression_statement_nodes)
    assert len(updated_expression_statement_nodes) >= 1


@pytest.mark.parametrize(
    'updated_tree_fixture_name',['updated_tree_removal', 'updated_tree_replacement', 'updated_tree_combination']
)
def test_c_call_expression_removal(initial_tree, updated_tree_fixture_name, request):
    updated_tree = request.getfixturevalue(updated_tree_fixture_name)
    initial_call_expression_nodes = find_nodes_of_type(
        initial_tree.root_node, "call_expression"
    )
    updated_call_expression_nodes = find_nodes_of_type(
        updated_tree.root_node, "call_expression"
    )
    # assert len(initial_call_expression_nodes) > 0
    if updated_tree_fixture_name == "updated_tree_replacement":
        assert len(updated_call_expression_nodes) == 2
        assert len(updated_call_expression_nodes) < len(initial_call_expression_nodes)
    else:
        assert len(updated_call_expression_nodes) == 0
        assert len(updated_call_expression_nodes) < len(initial_call_expression_nodes)


@pytest.mark.parametrize(
    'updated_tree_fixture_name',['updated_tree_removal', 'updated_tree_replacement', 'updated_tree_combination']
)
def test_c_for_statement_removal(initial_tree, updated_tree_fixture_name, request):
    updated_tree = request.getfixturevalue(updated_tree_fixture_name)
    initial_for_statement_nodes = find_nodes_of_type(
        initial_tree.root_node, "for_statement"
    )
    updated_for_statement_nodes = find_nodes_of_type(
        updated_tree.root_node, "for_statement"
    )
    assert len(initial_for_statement_nodes) > len(updated_for_statement_nodes)
    assert len(updated_for_statement_nodes) > 0
    for for_statement_node in updated_for_statement_nodes:
        _, line_num = REMOVAL_FOR_STATEMENT.split("_")
        assert str(for_statement_node.start_point[0]) != line_num


@pytest.mark.parametrize(
    'updated_tree_fixture_name',['updated_tree_removal', 'updated_tree_replacement', 'updated_tree_combination']
)
def test_c_if_statement_removal(initial_tree, updated_tree_fixture_name, request):
    updated_tree = request.getfixturevalue(updated_tree_fixture_name)
    initial_if_statement_nodes = find_nodes_of_type(
        initial_tree.root_node, "if_statement"
    )
    updated_if_statement_nodes = find_nodes_of_type(
        updated_tree.root_node, "if_statement"
    )
    assert len(initial_if_statement_nodes) > len(updated_if_statement_nodes)
    assert len(updated_if_statement_nodes) > 0
    for if_statement_node in updated_if_statement_nodes:
        _, line_num = REMOVAL_IF_STATEMENT.split("_")
        assert str(if_statement_node.start_point[0]) != line_num

@pytest.mark.parametrize(
    'updated_tree_fixture_name',['updated_tree_removal', 'updated_tree_replacement', 'updated_tree_combination']
)
def test_c_struct_specifier_removal(initial_tree, updated_tree_fixture_name, request):
    updated_tree = request.getfixturevalue(updated_tree_fixture_name)
    initial_struct_specifier_nodes = find_nodes_of_type(
        initial_tree.root_node, "struct_specifier"
    )
    updated_struct_specifier_nodes = find_nodes_of_type(
        updated_tree.root_node, "struct_specifier"
    )
    if updated_tree_fixture_name == "updated_tree_replacement":
        assert len(initial_struct_specifier_nodes) == len(updated_struct_specifier_nodes)
    else:
        assert len(initial_struct_specifier_nodes) > len(updated_struct_specifier_nodes)


# def test_c_removals_small_c(small_c_tree):
#     breakpoint()
