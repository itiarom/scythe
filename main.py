import argparse
import time
import resource
import sys

from reducer import utils
from reducer.dd import Interesting, perform_dd
from reducer.checker import PROPERTY_CHECKERS, JavaPropertyChecker
from reducer.graph import build_graph_from_file

resource.setrlimit(resource.RLIMIT_STACK, (2**29, -1))
sys.setrecursionlimit(10**6)


#example Solidity: greduce --source-file ./Solidity/smart2/ext_changed.sol --script ./Solidity/smart2/solidity2.sh
#example C: greduce --source-file "./C/gcc-59903/small.c" --script "./C/gcc-59903/test_r.sh" --language c --mode "$mode"
#example Java: greduce --source-file "./Java/generator_modified/iter_1/Main.java" --script "./Java/generator_modified/iter_1/run.sh" --language java --mode "$mode"

# Argument parsing
parser = argparse.ArgumentParser(
    description=('Modify Solidity files based on node removal and '
                 "Slither analysis, considering specified findings.")
)

parser.add_argument(
    "--language",
    default="solidity",
    choices=['solidity', 'c', 'java'],
    help="Select specific language (options: 'solidity', 'c', 'java')"
)

parser.add_argument(
    "--source-file",
    type=str,
    default="ext_changed.sol",
    help="Source file to minimize",
)

parser.add_argument(
    '--script',
    type=str,
    help='script to run"',
    default="./solidity2.sh"
)

parser.add_argument(
    "--mode",
    default="combination",
    choices=['removal', 'replacement', 'combination', 'break'],
    help="Select whether the removal of variables should follow a removal, "
         "replacement or a combination strategy"
)
args = parser.parse_args()


def main():
    start_time = time.time()
    file_path = args.source_file
    print(f"Using source file: {file_path}")

    graph = build_graph_from_file(file_path, args.language)
    print(f"Graph built from file: {file_path}")
    print(graph)

    print(args.script)
    print(file_path)
    prop_checker = PROPERTY_CHECKERS[args.language](file_path, args.script)
    content = utils.read_file(file_path)

    interesting = Interesting(graph, content,
                              prop_checker, args.language, args.mode)

    passes = [
        ["function", "modifier", "event"],
        ["contract", "struct"],
        ["state_var"],
        ["contract", "struct"],
        ["var"]
    ]
    parallel = True

    if args.language == "c":
        parallel = False
        passes = [
            ["for_statement", "if_statement"],
            ["global_variable", "struct"],
            ["function"],
            ["for_statement", "if_statement"],
        ]
        if args.mode not in ["removal", "replacement", "combination"]:
            raise ValueError(
                f"Unknown mode: {args.mode}. Must be 'removal', 'replacement', "
                f"or 'combination'."
            )
    elif args.language == "java":
        passes = ["class"]
        interesting.removal_mode = "break"
    else:
        if args.mode not in ["removal"]:
            raise ValueError(f"Unknown mode: {args.mode}. Must be 'removal'")

        # Inheritance-chain simplification: flatten base contracts into the
        # children that use their members, so the base can then be removed and
        # the de-shared members reduced individually by the passes below.
        interesting.removal_mode = "flatten"
        interesting.mode = ["contract"]
        perform_dd(interesting, lambda n: n.node_type == "contract",
                   parallel=parallel, language=args.language)
        interesting.removal_mode = "removal"
        graph = build_graph_from_file(file_path, args.language)
        interesting.graph = graph

    # Solidity iterates the passes to a fixed point (removals enable further
    # removals: e.g. dropping a local var's last use lets the var go on the next
    # round); c/java run the loop once. The graph is rebuilt per pass so it
    # matches the progressively reduced source.
    fixed_point = False
    while not fixed_point:
        before = utils.read_file(file_path)
        for pass_ in passes:
            if args.language in ("java", "solidity"):
                graph = build_graph_from_file(file_path, args.language)
                interesting.graph = graph

            interesting.mode = pass_
            perform_dd(interesting, lambda n: n.node_type in pass_,
                       parallel=parallel, language=args.language)
        fixed_point = (args.language != "solidity"
                       or utils.read_file(file_path) == before)

    if args.language == "java":
        graph = build_graph_from_file(file_path, args.language)
        interesting.graph = graph

        fixed_point_reached = False
        if args.mode == "combination":
            passes = [["function"], ["field"], ["local_variable"]]
            interesting.removal_mode = "replacement"

            counter = 0

            while not fixed_point_reached:
                old = utils.read_file(file_path)
                for pass_ in passes:
                    graph = build_graph_from_file(file_path, args.language)

                    interesting.graph = graph
                    interesting.mode = pass_
                    perform_dd(interesting, lambda n: n.node_type in pass_, parallel=True)

                new = utils.read_file(file_path)
                fixed_point_reached = (old == new)
                counter += 1
            passes = [
                ["local_variable"],
                ["function"],
                ["constructor"],
                ["field"],
                ["class"],
                ["local_variable", "function", "field"]

            ]
            fixed_point_reached = False
            remove_iteration_counter = 0

            while not fixed_point_reached:
                remove_iteration_counter += 1
                old_content = utils.read_file(file_path)
                for pass_ in passes:
                    graph = build_graph_from_file(file_path, args.language)
                    prop_checker = JavaPropertyChecker(file_path, args.script)
                    content = utils.read_file(file_path)
                    interesting = Interesting(graph, content,
                                              prop_checker,
                                              args.language, "removal")
                    interesting.mode = pass_
                    perform_dd(interesting, lambda n: n.node_type in pass_,
                               parallel=False)

                new_content = utils.read_file(file_path)
                if old_content == new_content:
                    fixed_point_reached = True
        elif args.mode == "removal":
            passes = [
                ["local_variable"],
                ["function"],
                ["constructor"],
                ["field"],
                ["class"],
            ]
            fixed_point_reached = False
            interesting.removal_mode = "removal"
            remove_iteration_counter = 0

            while not fixed_point_reached:
                remove_iteration_counter += 1
                old_content = utils.read_file(file_path)
                for pass_ in passes:
                    graph = build_graph_from_file(file_path, args.language)
                    prop_checker = JavaPropertyChecker(file_path, args.script)
                    content = utils.read_file(file_path)
                    interesting = Interesting(graph, content,
                                              prop_checker, args.language, "removal")
                    interesting.mode = pass_
                    perform_dd(interesting, lambda n: n.node_type in pass_,
                               parallel=False)
                new_content = utils.read_file(file_path)
                if old_content == new_content:
                    fixed_point_reached = True
        elif args.mode == "replacement":
            passes = [["function"], ["field"], ["local_variable"]]
            interesting.removal_mode = "replacement"

            counter = 0

            while not fixed_point_reached:
                old = utils.read_file(file_path)
                for pass_ in passes:
                    graph = build_graph_from_file(file_path, args.language)
                    interesting.graph = graph
                    interesting.mode = pass_
                    perform_dd(interesting, lambda n: n.node_type in pass_, parallel=True)
                new = utils.read_file(file_path)
                fixed_point_reached = (old == new)
                counter += 1

    end_time = time.time()
    elapsed_time = end_time - start_time
    print(f"Execution time: {elapsed_time} seconds")


if __name__ == "__main__":
    main()
