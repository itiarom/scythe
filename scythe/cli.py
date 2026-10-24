import argparse
import resource
import shlex
import subprocess
import sys
import time

from scythe import utils
from scythe.checker import PROPERTY_CHECKERS
from scythe.dd import Interesting, perform_dd, parallel_probe_reduce
from scythe.deferral import DeferralTracker
from scythe.graph import build_graph_from_file
from scythe.passes import LANGUAGES

resource.setrlimit(resource.RLIMIT_STACK, (2**29, -1))
sys.setrecursionlimit(10**6)


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
                             "further in place (e.g. a Perses wrapper), then "
                             "alternate scythe and CMD until neither changes "
                             "the file. Omitted = scythe behaves exactly as "
                             "without it.")
    parser.add_argument("--max-rounds", type=int, default=4,
                        help="Maximum scythe/post-processor alternations.")
    parser.add_argument("--scythe-output", metavar="PATH", default=None,
                        help="Also save the program produced by the first "
                             "scythe round (before any post-processor) here.")
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)
    return parser.parse_args()


def same_tokens(a, b):
    return a.split() == b.split()


def run_to_fixpoint(file_path, sweep, deferral=None):
    # A sweep that changed nothing but deferred some probes is followed by a
    # full sweep, so the fixpoint is the same as without deferral.
    full = False
    while True:
        before = utils.read_file(file_path)
        if deferral is not None:
            deferral.start_sweep(full)
        sweep()
        if utils.read_file(file_path) != before:
            full = False
        elif full or deferral is None or not deferral.skipped_this_sweep:
            return
        else:
            full = True


def reduce_program(interesting, file_path, language, fine=True):
    cfg = LANGUAGES[language]

    def sweep():
        for p in cfg.passes:
            if p.fine and not fine:
                continue
            interesting.graph = build_graph_from_file(file_path, language)
            interesting.removal_mode = p.mode or cfg.mode
            interesting.mode = list(p.kinds)
            node_filter = (
                lambda n, p=p: n.node_type in p.kinds
                and (p.where is None or p.where(n, interesting.graph)))
            if p.driver in ("probe", "bulk"):
                parallel_probe_reduce(interesting, node_filter,
                                      union_first=p.driver == "bulk")
            else:
                perform_dd(interesting, node_filter)

    run_to_fixpoint(file_path, sweep, interesting.deferral)


def main():
    args = parse_args()
    start = time.time()

    graph = build_graph_from_file(args.source_file, args.language)
    checker = PROPERTY_CHECKERS[args.language](args.source_file, args.script)
    content = utils.read_file(args.source_file)
    deferral = DeferralTracker() if LANGUAGES[args.language].defer else None
    interesting = Interesting(graph, content, checker, args.language, deferral)

    for round_no in range(max(1, args.max_rounds)):
        before = utils.read_file(args.source_file)
        t = time.time()
        # Parameters, locals and fields wait until the post-processor has run.
        fine = not args.post_processor or round_no > 0
        reduce_program(interesting, args.source_file, args.language, fine)
        after = utils.read_file(args.source_file)
        print(f"[round {round_no}] scythe {time.time() - t:.0f}s, "
              f"{len(before)} -> {len(after)} bytes", flush=True)
        if round_no == 0 and args.scythe_output:
            utils.update_file(args.scythe_output, after)
        if not args.post_processor or (round_no and same_tokens(after, before)):
            break
        before = after
        t = time.time()
        cmd = [*shlex.split(args.post_processor), args.source_file, args.script]
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL)
        if result.returncode != 0:
            utils.update_file(args.source_file, before)
            print(f"[round {round_no}] WARNING: post-processor failed "
                  f"(exit {result.returncode}); keeping the current program",
                  flush=True)
            break
        after = utils.read_file(args.source_file)
        print(f"[round {round_no}] post-processor {time.time() - t:.0f}s, "
              f"{len(before)} -> {len(after)} bytes", flush=True)
        if same_tokens(after, before):
            break
        interesting.content = after
        interesting.base_content = after

    print(f"Execution time: {time.time() - start} seconds")
    print(f"Oracle runs: {interesting.oracle_runs}"
          + (f", deferred probes: {deferral.skipped}" if deferral else ""))


if __name__ == "__main__":
    main()
