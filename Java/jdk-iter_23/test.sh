#!/bin/bash
JFLAGS="-proc:none -J-XX:+UseSerialGC -J-XX:TieredStopAtLevel=1 -J-XX:-UsePerfData"
CRASH_SIGNATURE='at com.sun.tools.javac.code.Symbol$VarSymbol.getConstValue(Symbol.java:'

source_file="${1:-$(find . -maxdepth 1 -name "*.java" | head -n 1)}"

if [ ! -f "$source_file" ]; then
    echo "Error: Could not find Java file in working directory."
    exit 1
fi

javac $JFLAGS "$source_file" 2> compile_err.txt
if ! grep -q "An exception has occurred in the compiler" compile_err.txt; then
    echo "Property does not hold: expected compiler crash not observed."
    cat compile_err.txt
    exit 1
fi
if ! grep -qF "$CRASH_SIGNATURE" compile_err.txt; then
    echo "Property does not hold: crash signature mismatch (a different bug)."
    cat compile_err.txt
    exit 1
fi

if [ -n "$REFERENCE_JAVAC" ]; then
    ref_out="$(mktemp -d)"
    if ! "$REFERENCE_JAVAC" $JFLAGS -d "$ref_out" "$source_file" > "$ref_out/ref_err.txt" 2>&1; then
        echo "Property does not hold: program does not compile with the reference javac."
        cat "$ref_out/ref_err.txt"
        rm -rf "$ref_out"
        exit 1
    fi
    rm -rf "$ref_out"
fi

exit 0
