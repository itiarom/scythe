import argparse
import resource
import shlex
import subprocess
import sys
import time

from scythe import utils
from scythe.checker import PROPERTY_CHECKERS
from scythe.dd import Interesting, perform_dd, parallel_probe_reduce
from scythe.graph import build_graph_from_file
from scythe.passes import LANGUAGES

resource.setrlimit(resource.RLIMIT_STACK, (2**29, -1))
sys.setrecursionlimit(10**6)

# example Solidity: scythe --source-file foo.sol  --script run.sh --language solidity
# example C:        scythe --source-file foo.c    --script run.sh --language c
# example Java:     scythe --source-file Main.java --script run.sh --language java


def parse_args():
    parser = argparse.ArgumentParser(
        description="Semantic-aware program reducer (scythe).")
    parser.add_argument("--language", default="solidity", choices=list(LANGUAGES),
                        help="Source language.")
    parser.add_argument("--source-file", required=True,
                        help="Source file to minimize (rewritten in place).")
    parser.add_argument("--script", required=True,
                        help="Property test script (exit 0 iff the property holds).")
    parser.add_argument("--post-processor", metavar="CMD", default=None,
                        help="After scythe reaches a fixpoint, run "
                             "'CMD <source-file> <script>' to reduce the file "
                             "further in place (e.g. a Perses wrapper). Omitted "
                             "= scythe behaves exactly as without it.")
    return parser.parse_args()


def run_to_fixpoint(file_path, sweep):
    """Run ``sweep`` until it stops shrinking the file."""
    while True:
        before = utils.read_file(file_path)
        sweep()
        if utils.read_file(file_path) == before:
            return


def reduce_program(interesting, file_path, language):
    """Run the language's pass schedule to a fixpoint. One driver, every
    language: rebuild the graph, then probe or delta-debug each pass's nodes."""
    cfg = LANGUAGES[language]

    def sweep():
        for p in cfg.passes:
            interesting.graph = build_graph_from_file(file_path, language)
            interesting.removal_mode = p.mode or cfg.mode
            interesting.mode = list(p.kinds)
            node_filter = (
                lambda n, p=p: n.node_type in p.kinds
                and (p.where is None or p.where(n)))
            if p.driver == "probe":
                parallel_probe_reduce(interesting, node_filter)
            else:
                perform_dd(interesting, node_filter, parallel=True)

    run_to_fixpoint(file_path, sweep)


def main():
    args = parse_args()
    start = time.time()

    graph = build_graph_from_file(args.source_file, args.language)
    checker = PROPERTY_CHECKERS[args.language](args.source_file, args.script)
    content = utils.read_file(args.source_file)
    interesting = Interesting(graph, content, checker, args.language,
                              LANGUAGES[args.language].mode)

    reduce_program(interesting, args.source_file, args.language)

    print(f"Execution time: {time.time() - start} seconds")

    if args.post_processor:
        cmd = [*shlex.split(args.post_processor), args.source_file, args.script]
        result = subprocess.run(cmd)
        if result.returncode != 0:
            sys.exit(result.returncode)


if __name__ == "__main__":
    main()
