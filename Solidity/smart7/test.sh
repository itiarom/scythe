#!/bin/bash
# Property check: slither must report "is never initialized" exactly 1 time(s).
# Invoked by scythe (passes the candidate path as $1) and by Perses (candidate
# staged as program.sol in its working dir, so no argument is passed).
source_file="${1:-program.sol}"
count=$(slither "$source_file" 2>&1 >/dev/null | grep -o "is never initialized" | wc -l | xargs)
[ "$count" -eq 1 ] && exit 0 || exit 1
