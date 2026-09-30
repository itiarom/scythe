#!/bin/bash
#
# Run the reduction benchmarks (Solidity, Java, or C) and collect the minimized
# programs for three methods, so a separate script can later measure token
# counts / performance.
#
#   scythe         : scythe on the original program
#   scythe+perses  : Perses on the program produced by scythe
#   perses          : baseline -- Perses on the original program
#
# For each benchmark <name> the following files are written under the output dir
# (the extension is .sol for Solidity, .java for Java):
#
#   <out>/<name>/original.<ext>
#   <out>/<name>/minimized_scythe.<ext>
#   <out>/<name>/minimized_scythe_perses.<ext>
#   <out>/<name>/minimized_perses.<ext>
#   <out>/<name>/time                       # one "method=seconds" line per method,
#                                           # incl. scythe_perses = scythe + its Perses pass
#
# Each benchmark dir holds exactly: original.<ext>, test.sh, version (C also
# keeps r.sh, the pristine creduce script, for provenance).
#   Solidity (Solidity/smart*/): version = solc version;       scythe --mode removal
#   Java     (Java/*/):          version = javac major (8/11);  scythe --mode replacement
#   C        (C/*/):             version = bug-compiler image;  scythe --mode replacement
#
# Java specifics: the `version` file selects a JDK via SDKMAN (auto-installed if
# missing). The oracle (test.sh) compiles with that JDK's `javac`, but Perses
# itself needs a modern JVM, so it is launched with a modern `java` while the
# benchmark JDK is placed first on PATH -- the oracle subprocess Perses spawns
# then resolves the right `javac` with no hardcoded paths.
#
# C specifics: the oracle pins its buggy/reference compilers via Docker, so the
# `version` file is the bug-triggering image (e.g. gcc-4.8); it cannot be
# auto-installed, and a missing image is a clean SKIP. The oracle bind-mounts the
# candidate from $(pwd), so candidates are staged under the working directory,
# which must be mounted into the Docker daemon at the same absolute path. Requires
# `docker` (the user must be able to run it without sudo). No comment stripping
# is applied.
#
# Usage:
#   ./run-benchmarks.sh [OPTIONS]
#     -l, --language LANG   solidity (default), java, or c
#     -o, --output DIR      Output directory (default: ./output)
#     -b, --benchmark NAME  Run a single benchmark (e.g. smart2 / jdk-bugs-iter_1)
#         --only-perses     Run only the Perses baseline
#         --only-scythe    Run scythe and scythe+perses only (skip the baseline)
#     -h, --help            Show this help

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PERSES_JAR="$ROOT_DIR/perses_deploy.jar"
DELETE_COMMENTS="$ROOT_DIR/delete_comments.py"

LANGUAGE="solidity"
OUTPUT_DIR="output"
BENCHMARK=""
ONLY_PERSES=false
ONLY_SCYTHE=false

# Resolved per benchmark (Java): the bin dir of the JDK its `version` selects.
BENCH_JDK_BIN=""
# Resolved once (Java): a modern (>=11) `java` to run Perses itself.
PERSES_JAVA=""
# Resolved once (Java): a non-buggy reference `javac` (>=11). The set-2 crash
# oracles use it to reject programs that no longer compile cleanly, so a reduced
# program stays a valid Java program that ONLY crashes the buggy javac.
REFERENCE_JAVAC=""

usage() { sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^#\s\?//'; exit "${1:-0}"; }

# ---- argument parsing -------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case $1 in
        -l|--language)  LANGUAGE="$2"; shift 2 ;;
        -o|--output)    OUTPUT_DIR="$2"; shift 2 ;;
        -b|--benchmark) BENCHMARK="$2"; shift 2 ;;
        --only-perses)  ONLY_PERSES=true; shift ;;
        --only-scythe) ONLY_SCYTHE=true; shift ;;
        -h|--help)      usage 0 ;;
        *) echo "Unknown parameter: $1" >&2; usage 1 ;;
    esac
done

if $ONLY_PERSES && $ONLY_SCYTHE; then
    echo "Error: --only-perses and --only-scythe are mutually exclusive." >&2
    exit 1
fi

# ---- per-language configuration ---------------------------------------------
case "$LANGUAGE" in
    solidity) BASE_DIR="Solidity"; EXT="sol";  BENCH_GLOB="smart*" ;;
    java)     BASE_DIR="Java";     EXT="java"; BENCH_GLOB="*" ;;
    c)        BASE_DIR="C";        EXT="c";    BENCH_GLOB="*" ;;
    *) echo "Error: --language must be 'solidity', 'java', or 'c' (got '$LANGUAGE')." >&2; exit 1 ;;
esac

# scythe: prefer the in-repo venv, fall back to PATH.
if [[ -x "$ROOT_DIR/.venv/bin/scythe" ]]; then
    SCYTHE="$ROOT_DIR/.venv/bin/scythe"
else
    SCYTHE="scythe"
fi

[[ -f "$PERSES_JAR" ]] || { echo "Error: $PERSES_JAR not found." >&2; exit 1; }
if [[ "$LANGUAGE" == "solidity" ]]; then
    command -v solc-select >/dev/null || { echo "Error: solc-select not found." >&2; exit 1; }
fi
if [[ "$LANGUAGE" == "c" ]]; then
    command -v docker >/dev/null || { echo "Error: docker not found (needed for the C oracles)." >&2; exit 1; }
    if [[ ! -S /var/run/docker.sock && -z "${DOCKER_HOST:-}" ]]; then
        echo "Error: C benchmarks require the Docker socket to be mounted at /var/run/docker.sock." >&2
        echo "Run with: -v /var/run/docker.sock:/var/run/docker.sock" >&2
        exit 1
    fi
    if ! docker info >/dev/null 2>&1; then
        echo "Error: Docker daemon is unavailable from inside the container. Mount /var/run/docker.sock or run on a Docker-enabled host." >&2
        exit 1
    fi
fi

ensure_c_compiler_images() {
    local dir="$1"
    local images=()
    local name

    if [[ -f "$dir/version" ]]; then
        while IFS= read -r name; do
            [[ -n "$name" ]] && images+=("$name")
        done < "$dir/version"
    fi

    if [[ ${#images[@]} -eq 0 ]]; then
        return 0
    fi

    # de-duplicate while preserving order
    local dedup=()
    local seen=()
    for image in "${images[@]}"; do
        if [[ " ${seen[*]} " != *" $image "* ]]; then
            dedup+=("$image")
            seen+=("$image")
        fi
    done
    images=("${dedup[@]}")

    local missing=()
    local image
    for image in "${images[@]}"; do
        if ! docker image inspect "$image" >/dev/null 2>&1; then
            missing+=("$image")
        fi
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "  SKIP $(basename "$dir"): missing C compiler image(s): ${missing[*]}" >&2
        echo "        Build them first with ./scripts/docker-setup.sh --language c --benchmark $(basename "$dir")" >&2
        return 1
    fi

    return 0
}

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

# Echo the bin dir of an installed SDKMAN JDK matching $1. A spec containing a
# vendor suffix (e.g. 8.0.282-trava) is an EXACT identifier -- required for the
# javac8 crash benchmarks, whose bug only exists in specific builds; a bare major
# (e.g. 11) matches any installed build of that major. Non-zero if none found.
jdk_bin_for() {
    local spec="$1" cand
    if [[ "$spec" == *-* ]]; then                       # exact identifier
        cand="$HOME/.sdkman/candidates/java/$spec"
        [[ -x "$cand/bin/javac" ]] && { echo "$cand/bin"; return 0; }
        return 1
    fi
    for cand in "$HOME"/.sdkman/candidates/java/"$spec".*/ \
                "$HOME"/.sdkman/candidates/java/"$spec"-*/; do
        cand="${cand%/}"
        [[ -d "$cand" && -x "$cand/bin/javac" ]] && { echo "$cand/bin"; return 0; }
    done
    return 1
}

# Ensure a JDK matching $1 exists, installing it via SDKMAN if needed. An exact
# identifier is installed verbatim; a bare major installs the first build SDKMAN
# lists for it. On success sets the global BENCH_JDK_BIN; non-zero if still
# unavailable (e.g. a pinned build that is "local only" / no longer downloadable).
ensure_jdk() {
    local spec="$1" bin id
    if bin="$(jdk_bin_for "$spec")"; then BENCH_JDK_BIN="$bin"; return 0; fi

    local init="$HOME/.sdkman/bin/sdkman-init.sh"
    if [[ -s "$init" ]]; then
        echo "  Installing JDK $spec via SDKMAN ..."
        set +u
        export sdkman_auto_answer=true sdkman_selfupdate_enable=false
        # shellcheck disable=SC1090
        source "$init"
        if [[ "$spec" == *-* ]]; then
            id="$spec"                                   # exact identifier
        else
            # first identifier whose version column starts with "<major>."
            id="$(sdk list java 2>/dev/null | awk -F'|' -v m="$spec" '
                NF>=6 { v=$3; gsub(/[ \t]/,"",v); idn=$6; gsub(/[ \t]/,"",idn);
                        if (v ~ "^"m"\\.") { print idn; exit } }')"
        fi
        [[ -n "$id" ]] && sdk install java "$id" >/dev/null 2>&1
        set -u
    fi
    if bin="$(jdk_bin_for "$spec")"; then BENCH_JDK_BIN="$bin"; return 0; fi
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

# Resolve a non-buggy reference `javac` (>=11) for the set-2 crash oracles. They
# use it to reject programs that no longer compile cleanly, so a reduced program
# stays a valid Java program that ONLY crashes the buggy javac. Prefer javac 11
# (the canonical reference, installing it if needed) without disturbing the
# per-benchmark JDK selection; fall back to the modern JDK that runs Perses.
resolve_reference_javac() {
    local bin saved
    if bin="$(jdk_bin_for 11)"; then REFERENCE_JAVAC="$bin/javac"; return 0; fi
    saved="$BENCH_JDK_BIN"
    if ensure_jdk 11; then
        bin="$BENCH_JDK_BIN"; BENCH_JDK_BIN="$saved"
        REFERENCE_JAVAC="$bin/javac"; return 0
    fi
    BENCH_JDK_BIN="$saved"
    if [[ -n "$PERSES_JAVA" ]]; then
        bin="$(dirname "$PERSES_JAVA")/javac"
        [[ -x "$bin" ]] && { REFERENCE_JAVAC="$bin"; return 0; }
    fi
    return 1
}

# =============================================================================
# Reduction drivers
# =============================================================================

# run_scythe <abs-source> <abs-test> ; reduces <source> in place, echoes seconds.
run_scythe() {
    local src="$1" test="$2" start end work
    start=$(date +%s)
    if [[ "$LANGUAGE" == "java" ]]; then
        # Run in a throwaway cwd so the oracle's javac output (compile_err.txt,
        # package class files) does not litter the repo, with the benchmark JDK
        # first on PATH so the oracle resolves the right javac.
        work="$(mktemp -d)"
        ( cd "$work" && PATH="$BENCH_JDK_BIN:$PATH" REFERENCE_JAVAC="$REFERENCE_JAVAC" \
            "$SCYTHE" --source-file "$src" --script "$test" \
                       --language java ) >/dev/null 2>&1
        rm -rf "$work"
    elif [[ "$LANGUAGE" == "c" ]]; then
        # The C oracles assume the candidate sits in $(pwd) (they bind-mount it
        # into the buggy-compiler container). Reduce a copy named program.c in a
        # workspace-mounted throwaway cwd so nested Docker can access it and the
        # oracle's byproducts never litter the repo.
        work="$(mktemp -d "$PWD/.scythe-work.XXXXXX")"
        cp "$src" "$work/program.c"
        ( cd "$work" && "$SCYTHE" --source-file program.c --script "$test" \
                       --language c ) >/dev/null 2>&1
        cp "$work/program.c" "$src"
        rm -rf "$work"
    else
        "$SCYTHE" --source-file "$src" --script "$test" >/dev/null 2>&1
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
    if [[ "$LANGUAGE" == "c" ]]; then
        work="$(mktemp -d "$PWD/.scythe-work.XXXXXX")"
    else
        work="$(mktemp -d)"
    fi
    persesout="$work/persesout"
    staged="$work/program.$EXT"
    cp "$input" "$staged"
    cp "$test_script" "$work/test.sh"
    chmod +x "$work/test.sh"
    start=$(date +%s)
    if [[ "$LANGUAGE" == "java" ]]; then
        # Perses runs on the modern JVM; the oracle it spawns inherits PATH, so
        # the benchmark JDK first on PATH gives the test script the right javac.
        PATH="$BENCH_JDK_BIN:$PATH" REFERENCE_JAVAC="$REFERENCE_JAVAC" "$PERSES_JAVA" -jar "$PERSES_JAR" \
            --test-script "$work/test.sh" \
            --input-file "$staged" \
            --output-dir "$persesout" >/dev/null 2>&1
    else
        # Solidity selects its solc; C needs no tool selection (the oracle pins
        # its compilers via Docker). Perses stages the candidate as program.$EXT
        # in its own cwd, which the test.sh resolves via ${1:-program.$EXT}.
        [[ "$LANGUAGE" == "solidity" ]] && solc-select use "$version" >/dev/null 2>&1
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
    elif [[ "$LANGUAGE" == "c" ]]; then
        # `version` is the bug-triggering Docker image (e.g. gcc-4.8). It cannot
        # be auto-installed; a missing image yields a clean SKIP. (A benchmark may
        # also need a second image for its reference compiler; if that one is
        # missing the original-program preflight below catches it.)
        docker image inspect "$version" >/dev/null 2>&1
    else
        ensure_jdk "$version"
    fi
}

# Run the oracle on a candidate with the benchmark's compiler active, in a
# throwaway cwd (for Java, contains javac output). Returns the oracle's exit code.
oracle_holds() {
    local test="$1" candidate="$2" rc work log
    if [[ "$LANGUAGE" == "java" ]]; then
        work="$(mktemp -d)"
        ( cd "$work" && PATH="$BENCH_JDK_BIN:$PATH" REFERENCE_JAVAC="$REFERENCE_JAVAC" bash "$test" "$candidate" ) >/dev/null 2>&1
        rc=$?
        rm -rf "$work"
    elif [[ "$LANGUAGE" == "c" ]]; then
        # Mirror the C oracle's "candidate lives in $(pwd)" contract: stage it as
        # program.c in a daemon-visible throwaway cwd and invoke the test with no
        # argument. The workspace must be mounted at the same absolute path.
        work="$(mktemp -d "$PWD/.scythe-work.XXXXXX")"
        log="$work/oracle.log"
        cp "$candidate" "$work/program.c"
        ( cd "$work" && bash "$test" ) >"$log" 2>&1
        rc=$?
        if [[ $rc -ne 0 ]]; then
            echo "  C oracle output:" >&2
            cat "$log" >&2
        fi
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

    if [[ "$LANGUAGE" == "c" ]] && ! ensure_c_compiler_images "$dir"; then
        return
    fi

    if ! select_compiler "$version"; then
        echo "  SKIP $name: compiler for version '$version' unavailable." >&2
        return
    fi

    # Stage a single shared input so every method starts from the same program.
    # Solidity strips comments once; Java is staged verbatim.
    local staged
    if [[ "$LANGUAGE" == "c" ]]; then
        staged="$(mktemp "$PWD/.scythe-stage.XXXXXX.$EXT")"
    else
        staged="$(mktemp --suffix=".$EXT")"
    fi
    cp "$original" "$staged"
    if [[ "$LANGUAGE" == "solidity" ]]; then
        python3 "$DELETE_COMMENTS" --filepath "$staged" >/dev/null 2>&1 \
            || python3 "$DELETE_COMMENTS" "$staged" >/dev/null 2>&1 || true
    fi

    # Pre-flight: the property must hold on the original, else nothing can be
    # reduced (Perses aborts at its own sanity check; scythe accepts no removal
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

    local scythe_time=0
    if ! $ONLY_PERSES; then
        local g_out="$out/minimized_scythe.$EXT"
        cp "$staged" "$g_out"
        echo "  [scythe] reducing..."
        scythe_time=$(run_scythe "$(cd "$out" && pwd)/minimized_scythe.$EXT" "$abs_test")
        echo "scythe=$scythe_time" >> "$timefile"
        echo "  [scythe] ${scythe_time}s -> $g_out"

        echo "  [scythe+perses] reducing scythe output with Perses..."
        local gp_time
        gp_time=$(run_perses "$(cd "$out" && pwd)/minimized_scythe.$EXT" \
                             "$out/minimized_scythe_perses.$EXT" "$abs_test" "$version")
        echo "scythe_perses=$((scythe_time + gp_time))" >> "$timefile"
        echo "  [scythe+perses] $((scythe_time + gp_time))s (scythe ${scythe_time}s + perses ${gp_time}s)"
    fi

    if ! $ONLY_SCYTHE; then
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
# --only-scythe) AND in scythe+perses (unless --only-perses); since those flags
# are mutually exclusive, Perses always runs for Java, so always resolve it.
if [[ "$LANGUAGE" == "java" ]]; then
    if ! resolve_perses_java; then
        echo "Error: no JDK >= 11 found to run Perses (install one via SDKMAN)." >&2
        exit 1
    fi
    if ! resolve_reference_javac; then
        echo "  WARN: no reference javac (>=11) resolved; the set-2 crash oracles" >&2
        echo "        will skip their compile-validity gate." >&2
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
