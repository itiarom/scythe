#!/bin/bash
# Property oracle. $1 = candidate path (scythe); otherwise the staged *.java in
# the cwd (Perses). Requires javac 11 on PATH (from the sibling `version` file).
# Pins the bug by its javac error CATEGORY (+ the intrinsic CAP# capture marker
# where the bug involves a capture) -- never by custom class/type names, so
# scythe's renaming/erasure cannot spuriously change the verdict. The invariant
# `category == total_errors` means EVERY emitted error is of the target kind.
source_file="${1:-$(find . -maxdepth 1 -name "*.java" | head -n 1)}"

if [ ! -f "$source_file" ]; then
    echo "Error: Could not find Java file in working directory."
    exit 1
fi

javac "$source_file" 2> compile_err.txt
total_errors=$(grep -c "error:" compile_err.txt)
category=$(grep -c "cannot be applied to given types" compile_err.txt)
reason=$(grep -cE "inference variable .* has incompatible bounds" compile_err.txt)
cap=$(grep -c "CAP#" compile_err.txt)

if [ "$total_errors" -ge 1 ] && [ "$category" -eq "$total_errors" ] && [ "$reason" -ge 1 ] && [ "$cap" -ge 1 ]; then
    exit 0
else
    exit 1
fi
