#!/bin/bash
# Property oracle. $1 = candidate path (greduce); otherwise the staged
# *.java in the cwd (Perses). Requires javac 8 on PATH (the benchmark
# harness selects it from the sibling `version` file).
#
# The harness also exports REFERENCE_JAVAC -- a non-buggy reference javac
# (>= 11). A program is only interesting if it (1) still compiles cleanly
# under that reference compiler AND (2) crashes the buggy javac 8. The
# reference gate rejects degenerate/invalid programs that happen to crash
# javac 8 for the wrong reason, so the reduced program stays a valid Java
# program that ONLY crashes the buggy compiler.
start_time=$(date +%s)

source_file="${1:-$(find . -maxdepth 1 -name "*.java" | head -n 1)}"

if [ ! -f "$source_file" ]; then
    echo "Error: Could not find Java file in working directory."
    exit 1
fi

# (1) Validity gate: must compile cleanly under the reference javac (e.g. 11).
# Class files go to a throwaway dir so they don't clash with the javac 8 pass.
if [ -n "$REFERENCE_JAVAC" ]; then
    ref_out="$(mktemp -d)"
    if ! "$REFERENCE_JAVAC" -d "$ref_out" "$source_file" > "$ref_out/ref_err.txt" 2>&1; then
        echo "Property does not hold: program does not compile with the reference javac."
        cat "$ref_out/ref_err.txt"
        rm -rf "$ref_out"
        exit 1
    fi
    rm -rf "$ref_out"
fi

# (2) Trigger: the buggy javac (8, first on PATH) must crash, capture stderr.
javac "$source_file" 2> compile_err.txt

# Check for crash pattern
if grep -q "An exception has occurred in the compiler" compile_err.txt; then
    exit 0
else
    echo "Property does not hold: expected compiler crash not observed."
    cat compile_err.txt
    exit 1
fi
