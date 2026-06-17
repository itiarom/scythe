#!/bin/bash
# Property oracle. $1 = candidate path (scythe); otherwise the staged
# *.java in the cwd (Perses). Requires javac 11 on PATH (the benchmark
# harness selects it from the sibling `version` file).
start_time=$(date +%s)

source_file="${1:-$(find . -maxdepth 1 -name "*.java" | head -n 1)}"
expected_error="error: type argument Function1<? extends Byte,? extends Byte> is not within bounds of type-variable A"
# javac 11.0.12 reports this diagnostic twice (and it is the only error), so the
# count is 2 on this build -- still "exactly the bug error and nothing else".
expected_count=2

if [ ! -f "$source_file" ]; then
    echo "Error: Could not find Java file in working directory."
    exit 1
fi

javac "$source_file" 2> compile_err.txt
count=$(grep -o "$expected_error" compile_err.txt | wc -l | xargs)
total_errors=$(grep -c "error:" compile_err.txt)

if [ "$count" -eq "$expected_count" ] && [ "$total_errors" -eq "$expected_count" ]; then
    exit 0
else
    exit 1
fi
