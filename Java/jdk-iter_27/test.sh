#!/bin/bash
# Property oracle. $1 = candidate path (greduce); otherwise the staged
# *.java in the cwd (Perses). Requires javac 8 on PATH (the benchmark
# harness selects it from the sibling `version` file).
start_time=$(date +%s)

source_file="${1:-$(find . -maxdepth 1 -name "*.java" | head -n 1)}"

if [ ! -f "$source_file" ]; then
    echo "Error: Could not find Java file in working directory."
    exit 1
fi

# Run using JDK 8u25, capture stderr
javac "$source_file" 2> compile_err.txt

# Check for crash pattern
if grep -q "An exception has occurred in the compiler" compile_err.txt; then
    exit 0
else
    echo "Property does not hold: expected compiler crash not observed."
    cat compile_err.txt
    exit 1
fi
