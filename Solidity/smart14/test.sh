#!/bin/bash
# Property check: slither must report "has external calls inside a loop" exactly 7 time(s).
# Invoked by scythe (passes the candidate path as $1) and by Perses (candidate
# staged as program.sol in its working dir, so no argument is passed).
source_file="${1:-program.sol}"
count=$(slither "$source_file" 2>&1 >/dev/null | grep -o "has external calls inside a loop" | wc -l | xargs)
[ "$count" -eq 7 ] && exit 0 || exit 1
