#!/bin/bash
# Property check: slither must report "contract sets array length with a user-controlled value" exactly 3 time(s).
# Invoked by scythe (passes the candidate path as $1) and by Perses (candidate
# staged as program.sol in its working dir, so no argument is passed).
source_file="${1:-program.sol}"
count=$(slither "$source_file" 2>&1 >/dev/null | grep -o "contract sets array length with a user-controlled value" | wc -l | xargs)
[ "$count" -eq 3 ] && exit 0 || exit 1
