#!/bin/bash
#
# Run the reduction benchmarks (Solidity or Java) and collect the minimized
# programs for three methods, so a separate script can later measure token
# counts / performance.
#
#   greduce         : greduce on the original program
#   greduce+perses  : Perses on the program produced by greduce
#   perses          : baseline -- Perses on the original program
#
# For each benchmark <name> the following files are written under the output dir
# (the extension is .sol for Solidity, .java for Java):
#
#   <out>/<name>/original.<ext>
#   <out>/<name>/minimized_greduce.<ext>
#   <out>/<name>/minimized_greduce_perses.<ext>
#   <out>/<name>/minimized_perses.<ext>
#   <out>/<name>/time                       # one "method=seconds" line per method,
#                                           # incl. greduce_perses = greduce + its Perses pass
#
# Each benchmark dir holds exactly: original.<ext>, test.sh, version.
#   Solidity (Solidity/smart*/): version = solc version;       greduce --mode removal
#   Java     (Java/*/):          version = javac major (8/11);  greduce --mode replacement
#
# Java specifics: the `version` file selects a JDK via SDKMAN (auto-installed if
# missing). The oracle (test.sh) compiles with that JDK's `javac`, but Perses
# itself needs a modern JVM, so it is launched with a modern `java` while the
# benchmark JDK is placed first on PATH -- the oracle subprocess Perses spawns
# then resolves the right `javac` with no hardcoded paths.
#
# Usage:
#   ./run-benchmarks.sh [OPTIONS]
#     -l, --language LANG   solidity (default) or java
#     -o, --output DIR      Output directory (default: ./output)
#     -b, --benchmark NAME  Run a single benchmark (e.g. smart2 / jdk-bugs-iter_1)
#         --only-perses     Run only the Perses baseline
#         --only-greduce    Run greduce and greduce+perses only (skip the baseline)
#     -h, --help            Show this help

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PERSES_JAR="$ROOT_DIR/perses_deploy.jar"
DELETE_COMMENTS="$ROOT_DIR/delete_comments.py"

LANGUAGE="solidity"
OUTPUT_DIR="output"
BENCHMARK=""
ONLY_PERSES=false
ONLY_GREDUCE=false

# Resolved per benchmark (Java): the bin dir of the JDK its `version` selects.
BENCH_JDK_BIN=""
# Resolved once (Java): a modern (>=11) `java` to run Perses itself.
PERSES_JAVA=""

usage() { sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^#\s\?//'; exit "${1:-0}"; }

# ---- argument parsing -------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case $1 in
        -l|--language)  LANGUAGE="$2"; shift 2 ;;
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

# ---- per-language configuration ---------------------------------------------
case "$LANGUAGE" in
    solidity) BASE_DIR="Solidity"; EXT="sol";  BENCH_GLOB="smart*"; GREDUCE_MODE="removal" ;;
    java)     BASE_DIR="Java";     EXT="java"; BENCH_GLOB="*";      GREDUCE_MODE="replacement" ;;
    *) echo "Error: --language must be 'solidity' or 'java' (got '$LANGUAGE')." >&2; exit 1 ;;
esac

# greduce: prefer the in-repo venv, fall back to PATH.
if [[ -x "$ROOT_DIR/.venv/bin/greduce" ]]; then
    GREDUCE="$ROOT_DIR/.venv/bin/greduce"
else
    GREDUCE="greduce"
fi

[[ -f "$PERSES_JAR" ]] || { echo "Error: $PERSES_JAR not found." >&2; exit 1; }
if [[ "$LANGUAGE" == "solidity" ]]; then
    command -v solc-select >/dev/null || { echo "Error: solc-select not found." >&2; exit 1; }
fi

# =============================================================================
# Solidity compiler (solc) provisioning
# =============================================================================

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

# =============================================================================
# Java compiler (JDK) provisioning via SDKMAN
# =============================================================================

# Echo the bin dir of an installed SDKMAN JDK whose major version matches $1
# (e.g. 11 -> .../11.0.12-open/bin), or return non-zero if none is installed.
jdk_bin_for_major() {
    local major="$1" cand
    for cand in "$HOME"/.sdkman/candidates/java/"$major".*/ \
                "$HOME"/.sdkman/candidates/java/"$major"-*/; do
        cand="${cand%/}"
        [[ -d "$cand" && -x "$cand/bin/javac" ]] && { echo "$cand/bin"; return 0; }
    done
    return 1
}

# Ensure a JDK for major version $1 exists, installing it via SDKMAN if needed.
# On success sets the global BENCH_JDK_BIN to its bin dir; returns non-zero if
# the JDK is still unavailable afterwards.
ensure_jdk() {
    local major="$1" bin id
    if bin="$(jdk_bin_for_major "$major")"; then BENCH_JDK_BIN="$bin"; return 0; fi

    local init="$HOME/.sdkman/bin/sdkman-init.sh"
    if [[ -s "$init" ]]; then
        echo "  Installing a JDK $major via SDKMAN ..."
        set +u
        export sdkman_auto_answer=true sdkman_selfupdate_enable=false
        # shellcheck disable=SC1090
        source "$init"
        # First identifier whose version column starts with "<major>."
        id="$(sdk list java 2>/dev/null | awk -F'|' -v m="$major" '
            NF>=6 { v=$3; gsub(/[ \t]/,"",v); idn=$6; gsub(/[ \t]/,"",idn);
                    if (v ~ "^"m"\\.") { print idn; exit } }')"
        [[ -n "$id" ]] && sdk install java "$id" >/dev/null 2>&1
        set -u
    fi
    if bin="$(jdk_bin_for_major "$major")"; then BENCH_JDK_BIN="$bin"; return 0; fi
    return 1
}

# Resolve a modern (>=11) `java` to run Perses with (Perses needs a recent JVM
# even when the benchmark compiles with an old javac). Resolve to a real path so
# it is unaffected by SDKMAN's `current` symlink changing during auto-install.
is_modern_java() { "$1" -version 2>&1 | grep -qE 'version "(1[1-9]|[2-9][0-9])'; }
resolve_perses_java() {
    local j cand bin
    j="$(command -v java 2>/dev/null || true)"
    [[ -n "$j" ]] && j="$(readlink -f "$j")"
    if [[ -n "$j" ]] && is_modern_java "$j"; then PERSES_JAVA="$j"; return 0; fi
    for cand in "$HOME"/.sdkman/candidates/java/*/; do
        bin="${cand}bin/java"
        if [[ -x "$bin" ]] && is_modern_java "$bin"; then
            PERSES_JAVA="$(readlink -f "$bin")"; return 0
        fi
    done
    return 1
}

# =============================================================================
# Reduction drivers
# =============================================================================

# run_greduce <abs-source> <abs-test> ; reduces <source> in place, echoes seconds.
run_greduce() {
    local src="$1" test="$2" start end work
    start=$(date +%s)
    if [[ "$LANGUAGE" == "java" ]]; then
        # Run in a throwaway cwd so the oracle's javac output (compile_err.txt,
        # package class files) does not litter the repo, with the benchmark JDK
        # first on PATH so the oracle resolves the right javac.
        work="$(mktemp -d)"
        ( cd "$work" && PATH="$BENCH_JDK_BIN:$PATH" \
            "$GREDUCE" --source-file "$src" --script "$test" \
                       --language java --mode "$GREDUCE_MODE" ) >/dev/null 2>&1
        rm -rf "$work"
    else
        "$GREDUCE" --source-file "$src" --script "$test" \
                   --mode "$GREDUCE_MODE" >/dev/null 2>&1
    fi
    end=$(date +%s)
    echo $((end - start))
}

# run_perses <abs-input> <abs-output> <abs-test> <version>
# Reduces <input> with Perses and copies the result to <output>. Echoes elapsed
# seconds on stdout. Perses requires ABSOLUTE paths (a relative --input-file
# crashes in createCurrentBestResultFolder) and writes <outdir>/<input-basename>.
run_perses() {
    local input="$1" output="$2" test_script="$3" version="$4"
    local work persesout staged start end
    work="$(mktemp -d)"
    persesout="$work/persesout"
    staged="$work/program.$EXT"
    cp "$input" "$staged"
    cp "$test_script" "$work/test.sh"
    chmod +x "$work/test.sh"
    start=$(date +%s)
    if [[ "$LANGUAGE" == "java" ]]; then
        # Perses runs on the modern JVM; the oracle it spawns inherits PATH, so
        # the benchmark JDK first on PATH gives the test script the right javac.
        PATH="$BENCH_JDK_BIN:$PATH" "$PERSES_JAVA" -jar "$PERSES_JAR" \
            --test-script "$work/test.sh" \
            --input-file "$staged" \
            --output-dir "$persesout" >/dev/null 2>&1
    else
        solc-select use "$version" >/dev/null 2>&1
        java -jar "$PERSES_JAR" \
            --test-script "$work/test.sh" \
            --input-file "$staged" \
            --output-dir "$persesout" >/dev/null 2>&1
    fi
    end=$(date +%s)
    if [[ -f "$persesout/program.$EXT" ]]; then
        cp "$persesout/program.$EXT" "$output"
    else
        echo "  WARN: Perses produced no output for $(basename "$(dirname "$output")")" >&2
    fi
    rm -rf "$work"
    echo $((end - start))
}

# Select the compiler this benchmark needs. Solidity: solc-select use (global
# state). Java: set BENCH_JDK_BIN (placed on PATH by the drivers/preflight).
# Returns non-zero if the required compiler is unavailable.
select_compiler() {
    local version="$1"
    if [[ "$LANGUAGE" == "solidity" ]]; then
        if ! solc_installed "$version"; then
            echo "  Installing solc $version ..."
            ensure_solc "$version" || true
        fi
        solc-select use "$version" >/dev/null 2>&1
    else
        ensure_jdk "$version"
    fi
}

# Run the oracle on a candidate with the benchmark's compiler active, in a
# throwaway cwd (for Java, contains javac output). Returns the oracle's exit code.
oracle_holds() {
    local test="$1" candidate="$2" rc work
    if [[ "$LANGUAGE" == "java" ]]; then
        work="$(mktemp -d)"
        ( cd "$work" && PATH="$BENCH_JDK_BIN:$PATH" bash "$test" "$candidate" ) >/dev/null 2>&1
        rc=$?
        rm -rf "$work"
    else
        bash "$test" "$candidate" >/dev/null 2>&1
        rc=$?
    fi
    return $rc
}

run_benchmark() {
    local dir="$1"
    local name; name="$(basename "$dir")"
    local original="$dir/original.$EXT"
    local test_script="$dir/test.sh"

    if [[ ! -f "$original" || ! -f "$test_script" ]]; then
        echo "Skipping $name: missing original.$EXT or test.sh." >&2; return
    fi
    if [[ ! -f "$dir/version" ]]; then
        echo "Skipping $name: no version file." >&2; return
    fi
    local version; version="$(cat "$dir/version")"

    echo "=== $name ($LANGUAGE, version $version) ==="
    local abs_test; abs_test="$(cd "$dir" && pwd)/test.sh"

    if ! select_compiler "$version"; then
        echo "  SKIP $name: compiler for version '$version' unavailable." >&2
        return
    fi

    # Stage a single shared input so every method starts from the same program.
    # Solidity strips comments once; Java is staged verbatim.
    local staged; staged="$(mktemp --suffix=".$EXT")"
    cp "$original" "$staged"
    if [[ "$LANGUAGE" == "solidity" ]]; then
        python3 "$DELETE_COMMENTS" --filepath "$staged" >/dev/null 2>&1 \
            || python3 "$DELETE_COMMENTS" "$staged" >/dev/null 2>&1 || true
    fi

    # Pre-flight: the property must hold on the original, else nothing can be
    # reduced (Perses aborts at its own sanity check; greduce accepts no removal
    # and returns the input unchanged). Turns a wrong compiler / incompatible
    # tool into a clear skip instead of silent empty output.
    if ! oracle_holds "$abs_test" "$staged"; then
        echo "  SKIP $name: property check fails on the original (test.sh exit != 0)." >&2
        echo "        Check that the compiler for version '$version' is correct and that the" >&2
        echo "        expected finding/error is still produced." >&2
        rm -f "$staged"
        return
    fi

    local out="$OUTPUT_DIR/$name"
    mkdir -p "$out"
    # Keep the program the methods reduced from, so the output dir is
    # self-contained for token/performance analysis.
    cp "$staged" "$out/original.$EXT"
    local timefile="$out/time"; : > "$timefile"

    local greduce_time=0
    if ! $ONLY_PERSES; then
        local g_out="$out/minimized_greduce.$EXT"
        cp "$staged" "$g_out"
        echo "  [greduce] reducing..."
        greduce_time=$(run_greduce "$(cd "$out" && pwd)/minimized_greduce.$EXT" "$abs_test")
        echo "greduce=$greduce_time" >> "$timefile"
        echo "  [greduce] ${greduce_time}s -> $g_out"

        echo "  [greduce+perses] reducing greduce output with Perses..."
        local gp_time
        gp_time=$(run_perses "$(cd "$out" && pwd)/minimized_greduce.$EXT" \
                             "$out/minimized_greduce_perses.$EXT" "$abs_test" "$version")
        echo "greduce_perses=$((greduce_time + gp_time))" >> "$timefile"
        echo "  [greduce+perses] $((greduce_time + gp_time))s (greduce ${greduce_time}s + perses ${gp_time}s)"
    fi

    if ! $ONLY_GREDUCE; then
        echo "  [perses] baseline reducing original with Perses..."
        local p_time
        p_time=$(run_perses "$staged" "$out/minimized_perses.$EXT" "$abs_test" "$version")
        echo "perses=$p_time" >> "$timefile"
        echo "  [perses] ${p_time}s -> $out/minimized_perses.$EXT"
    fi

    rm -f "$staged"
}

# ---- main -------------------------------------------------------------------
mkdir -p "$OUTPUT_DIR"

# Java needs a modern JVM to run Perses. Perses runs in the baseline (unless
# --only-greduce) AND in greduce+perses (unless --only-perses); since those flags
# are mutually exclusive, Perses always runs for Java, so always resolve it.
if [[ "$LANGUAGE" == "java" ]]; then
    if ! resolve_perses_java; then
        echo "Error: no JDK >= 11 found to run Perses (install one via SDKMAN)." >&2
        exit 1
    fi
fi

if [[ -n "$BENCHMARK" ]]; then
    dir="$BASE_DIR/$(basename "$BENCHMARK")"
    [[ -d "$dir" ]] || { echo "Error: benchmark dir '$dir' not found." >&2; exit 1; }
    run_benchmark "$dir"
else
    for dir in "$BASE_DIR"/$BENCH_GLOB/; do
        [[ -d "$dir" ]] && run_benchmark "${dir%/}"
    done
fi

echo "Done. Results in: $OUTPUT_DIR"
