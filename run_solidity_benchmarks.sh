#!/bin/bash
#
# Run the Solidity reduction benchmarks and collect the minimized programs for
# three methods, so a separate script can later measure token counts / performance.
#
#   greduce         : greduce on the original program
#   greduce+perses  : Perses on the program produced by greduce
#   perses          : baseline -- Perses on the original program
#
# For each benchmark <name> the following files are written under the output dir:
#
#   <out>/<name>/minimized_greduce.sol
#   <out>/<name>/minimized_greduce_perses.sol
#   <out>/<name>/minimized_perses.sol
#   <out>/<name>/time                       # one "method=seconds" line per method run
#                                           # greduce_perses = greduce + its Perses pass
#
# Each benchmark dir (Solidity/smart*/) holds exactly: original.sol, test.sh, version.
#
# Usage:
#   ./run_solidity_benchmarks.sh [OPTIONS]
#     -o, --output DIR      Output directory (default: ./output)
#     -b, --benchmark NAME  Run a single benchmark (e.g. smart2); default: all Solidity/smart*
#         --only-perses     Run only the Perses baseline
#         --only-greduce    Run greduce and greduce+perses only (skip the baseline)
#     -h, --help            Show this help

set -uo pipefail

BASE_DIR="Solidity"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PERSES_JAR="$ROOT_DIR/perses_deploy.jar"
DELETE_COMMENTS="$ROOT_DIR/delete_comments.py"

OUTPUT_DIR="output"
BENCHMARK=""
ONLY_PERSES=false
ONLY_GREDUCE=false

usage() { sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^#\s\?//'; exit "${1:-0}"; }

# ---- argument parsing -------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case $1 in
        -o|--output)    OUTPUT_DIR="$2"; shift 2 ;;
        -b|--benchmark) BENCHMARK="$2"; shift 2 ;;
        --only-perses)  ONLY_PERSES=true; shift ;;
        --only-greduce) ONLY_GREDUCE=true; shift ;;
        -h|--help)      usage 0 ;;
        *) echo "Unknown parameter: $1" >&2; usage 1 ;;
    esac
done

if $ONLY_PERSES && $ONLY_GREDUCE; then
    echo "Error: --only-perses and --only-greduce are mutually exclusive." >&2
    exit 1
fi

# greduce: prefer the in-repo venv, fall back to PATH.
if [[ -x "$ROOT_DIR/.venv/bin/greduce" ]]; then
    GREDUCE="$ROOT_DIR/.venv/bin/greduce"
else
    GREDUCE="greduce"
fi

command -v solc-select >/dev/null || { echo "Error: solc-select not found." >&2; exit 1; }
[[ -f "$PERSES_JAR" ]] || { echo "Error: $PERSES_JAR not found." >&2; exit 1; }

# True if solc-select already has the given version installed.
solc_installed() {
    solc-select versions 2>/dev/null | sed 's/[[:space:]].*//' | grep -Fxq "$1"
}

# binaries.soliditylang.org platform dir for the current OS.
solc_platform() {
    case "$(uname -s)" in
        Darwin) echo "macosx-amd64" ;;
        *)      echo "linux-amd64" ;;
    esac
}

# solc-select's artifacts dir (mirrors solc_select/constants.py: $VIRTUAL_ENV if
# set, otherwise $HOME).
solc_artifacts_dir() {
    echo "${VIRTUAL_ENV:-$HOME}/.solc-select/artifacts"
}

# Download a solc binary directly (with a normal User-Agent) and drop it where
# solc-select expects it. Works around Cloudflare on binaries.soliditylang.org
# blocking solc-select's default Python-urllib User-Agent with HTTP 403.
curl_install_solc() {
    local v="$1" platform base artifact dest
    command -v curl >/dev/null 2>&1 || return 1
    platform="$(solc_platform)"
    base="https://binaries.soliditylang.org/$platform"
    artifact="$(curl -fsSL "$base/list.json" 2>/dev/null \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['releases'].get('$v',''))" 2>/dev/null)"
    [[ -n "$artifact" ]] || return 1
    dest="$(solc_artifacts_dir)/solc-$v"
    mkdir -p "$dest"
    curl -fsSL -o "$dest/solc-$v" "$base/$artifact" 2>/dev/null || return 1
    chmod 0775 "$dest/solc-$v"
    solc_installed "$v"
}

# Install a solc version if it isn't installed already. Tries solc-select first,
# then falls back to a direct curl download (solc-select's downloader is blocked
# by Cloudflare). Returns non-zero if the version is still unavailable afterwards.
ensure_solc() {
    local v="$1"
    solc_installed "$v" && return 0
    solc-select install "$v" >/dev/null 2>&1
    solc_installed "$v" && return 0
    curl_install_solc "$v"
}

# Returns wall-clock seconds of a command (command output is discarded).
timed() {
    local start end
    start=$(date +%s)
    "$@" >/dev/null 2>&1
    end=$(date +%s)
    echo $((end - start))
}

# run_perses <abs-input.sol> <abs-output.sol> <abs-test.sh> <version>
# Reduces <input> with Perses and copies the result to <output>. Echoes elapsed
# seconds on stdout. Perses requires ABSOLUTE paths (a relative --input-file
# crashes in createCurrentBestResultFolder) and writes <outdir>/<input-basename>.
run_perses() {
    local input="$1" output="$2" test_script="$3" version="$4"
    local work persesout start end
    work="$(mktemp -d)"
    persesout="$work/persesout"
    cp "$input" "$work/program.sol"
    cp "$test_script" "$work/test.sh"
    chmod +x "$work/test.sh"
    solc-select use "$version" >/dev/null 2>&1
    start=$(date +%s)
    java -jar "$PERSES_JAR" \
        --test-script "$work/test.sh" \
        --input-file "$work/program.sol" \
        --output-dir "$persesout" >/dev/null 2>&1
    end=$(date +%s)
    if [[ -f "$persesout/program.sol" ]]; then
        cp "$persesout/program.sol" "$output"
    else
        echo "  WARN: Perses produced no output for $(basename "$(dirname "$output")")" >&2
    fi
    rm -rf "$work"
    echo $((end - start))
}

run_benchmark() {
    local dir="$1"
    local name; name="$(basename "$dir")"
    local original="$dir/original.sol"
    local test_script="$dir/test.sh"

    if [[ ! -f "$original" || ! -f "$test_script" ]]; then
        echo "Skipping $name: missing original.sol or test.sh." >&2; return
    fi
    if [[ ! -f "$dir/version" ]]; then
        echo "Skipping $name: no version file." >&2; return
    fi
    local version; version="$(cat "$dir/version")"

    echo "=== $name (solc $version) ==="
    local abs_test; abs_test="$(cd "$dir" && pwd)/test.sh"

    # Select the compiler this benchmark needs, installing it on demand.
    if ! solc_installed "$version"; then
        echo "  Installing solc $version ..."
        ensure_solc "$version" || true
    fi
    if ! solc-select use "$version" >/dev/null 2>&1; then
        echo "  SKIP $name: solc $version unavailable (install failed -- no network?)." >&2
        return
    fi

    # Stage a single shared input (comments stripped once) so every method starts
    # from the same program.
    local staged; staged="$(mktemp --suffix=.sol)"
    cp "$original" "$staged"
    python3 "$DELETE_COMMENTS" --filepath "$staged" >/dev/null 2>&1 \
        || python3 "$DELETE_COMMENTS" "$staged" >/dev/null 2>&1 || true

    # Pre-flight: the property must hold on the original, else nothing can be reduced
    # (Perses aborts at its own sanity check, greduce accepts no removal and returns
    # the input unchanged in ~0s). This turns a wrong/missing solc version or an
    # incompatible slither into a clear skip instead of silent empty output.
    if ! bash "$abs_test" "$staged" >/dev/null 2>&1; then
        echo "  SKIP $name: property check fails on the original (test.sh exit != 0)." >&2
        echo "        Check that solc $version is correct and that slither still reports the expected finding/count." >&2
        rm -f "$staged"
        return
    fi

    local out="$OUTPUT_DIR/$name"
    mkdir -p "$out"
    # Keep the (comment-stripped) program the methods reduced from, so the output
    # dir is self-contained for token/performance analysis.
    cp "$staged" "$out/original.sol"
    local timefile="$out/time"; : > "$timefile"

    local greduce_time=0
    if ! $ONLY_PERSES; then
        # greduce minimizes its --source-file in place.
        local g_out="$out/minimized_greduce.sol"
        cp "$staged" "$g_out"
        echo "  [greduce] reducing..."
        # Solidity reduction requires --mode removal (main.py rejects the default
        # "combination" mode for solidity).
        greduce_time=$(timed "$GREDUCE" --source-file "$g_out" --script "$abs_test" --mode removal)
        echo "greduce=$greduce_time" >> "$timefile"
        echo "  [greduce] ${greduce_time}s -> $g_out"

        echo "  [greduce+perses] reducing greduce output with Perses..."
        local gp_time
        gp_time=$(run_perses "$(cd "$out" && pwd)/minimized_greduce.sol" \
                             "$out/minimized_greduce_perses.sol" "$abs_test" "$version")
        echo "greduce_perses=$((greduce_time + gp_time))" >> "$timefile"
        echo "  [greduce+perses] $((greduce_time + gp_time))s (greduce ${greduce_time}s + perses ${gp_time}s)"
    fi

    if ! $ONLY_GREDUCE; then
        echo "  [perses] baseline reducing original with Perses..."
        local p_time
        p_time=$(run_perses "$staged" "$out/minimized_perses.sol" "$abs_test" "$version")
        echo "perses=$p_time" >> "$timefile"
        echo "  [perses] ${p_time}s -> $out/minimized_perses.sol"
    fi

    rm -f "$staged"
}

# ---- main -------------------------------------------------------------------
mkdir -p "$OUTPUT_DIR"

if [[ -n "$BENCHMARK" ]]; then
    dir="$BASE_DIR/$(basename "$BENCHMARK")"
    [[ -d "$dir" ]] || { echo "Error: benchmark dir '$dir' not found." >&2; exit 1; }
    run_benchmark "$dir"
else
    for dir in "$BASE_DIR"/smart*/; do
        [[ -d "$dir" ]] && run_benchmark "${dir%/}"
    done
fi

echo "Done. Results in: $OUTPUT_DIR"
