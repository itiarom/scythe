#!/usr/bin/env python3
"""Analyze a run_solidity_benchmarks.sh output directory.

For every benchmark in the output dir it reads the reduced programs and the
per-method timings and reports a JSON object:

    {
      "<benchmark>": {
        "original":       {"tokens": <int>},
        "perses":         {"tokens": <int>, "time": <seconds>},
        "greduce":        {"tokens": <int>, "time": <seconds>},
        "greduce-perses": {"tokens": <int>, "time": <seconds>}
      },
      ...
    }

Tokens are counted with tree-sitter (terminal nodes, excluding comments), the
same parser greduce uses. A method is omitted for a benchmark if its reduced
file is absent (e.g. it was not run). The output dir is self-contained:
run_solidity_benchmarks.sh writes `original.sol` (the program the methods were
reduced from) alongside the minimized files and `time`.

Usage:
    analyze_solidity_results.py [OUTPUT_DIR] [-o results.json]
    (default OUTPUT_DIR: ./output; prints to stdout if -o is omitted)
"""
import argparse
import json
import os
import sys

from reducer import parsers

# method name in the JSON -> (reduced-file name, key in the `time` file)
METHODS = {
    "perses": ("minimized_perses.sol", "perses"),
    "greduce": ("minimized_greduce.sol", "greduce"),
    "greduce-perses": ("minimized_greduce_perses.sol", "greduce_perses"),
}


def count_tokens(path):
    """Number of terminal tokens in a Solidity file (comments excluded)."""
    with open(path, "rb") as f:
        tree = parsers.PARSERS["solidity"].parse(f.read())
    tokens = 0
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.children:
            stack.extend(node.children)
        elif node.type != "comment":
            tokens += 1
    return tokens


def read_times(time_file):
    """Parse the `time` file ('method=seconds' per line) into {method: seconds}."""
    times = {}
    if not os.path.isfile(time_file):
        return times
    with open(time_file) as f:
        for line in f:
            line = line.strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            try:
                times[key.strip()] = int(value)
            except ValueError:
                try:
                    times[key.strip()] = float(value)
                except ValueError:
                    pass
    return times


def analyze_benchmark(bench_dir):
    original = os.path.join(bench_dir, "original.sol")
    if not os.path.isfile(original):
        return None
    entry = {"original": {"tokens": count_tokens(original)}}
    times = read_times(os.path.join(bench_dir, "time"))
    for method, (reduced_name, time_key) in METHODS.items():
        reduced = os.path.join(bench_dir, reduced_name)
        if not os.path.isfile(reduced):
            continue
        result = {"tokens": count_tokens(reduced)}
        if time_key in times:
            result["time"] = times[time_key]
        entry[method] = result
    return entry


def analyze(output_dir):
    results = {}
    for name in sorted(os.listdir(output_dir)):
        bench_dir = os.path.join(output_dir, name)
        if not os.path.isdir(bench_dir):
            continue
        entry = analyze_benchmark(bench_dir)
        if entry is not None:
            results[name] = entry
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output_dir", nargs="?", default="output",
                        help="run_solidity_benchmarks.sh output directory")
    parser.add_argument("-o", "--output", help="write JSON here (default: stdout)")
    args = parser.parse_args()

    if not os.path.isdir(args.output_dir):
        sys.exit(f"Error: output directory '{args.output_dir}' not found")

    results = analyze(args.output_dir)
    payload = json.dumps(results, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(payload + "\n")
        print(f"Wrote {len(results)} benchmark(s) to {args.output}")
    else:
        print(payload)


if __name__ == "__main__":
    main()
