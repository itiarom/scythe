#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="${SCYTHE_ROOT_DIR:-/scythe}"
if [[ ! -d "$ROOT_DIR" ]]; then
  ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

usage() {
  cat <<'EOF'
Usage:
  docker run --rm -it -v "$PWD:/workspace" -w /workspace IMAGE [COMMAND] [ARGS...]

Commands:
  scythe [ARGS...]                Run the reducer directly
  benchmark [ARGS...]            Run the repo benchmark runner (single or all examples)
  help                           Show this help message

Examples:
  docker run --rm -it -v "$PWD:/workspace" -w /workspace scythe \
    --source-file ./Solidity/smart2/original.sol --script ./Solidity/smart2/test.sh --language solidity

  docker run --rm -it -v "$PWD:/workspace" -w /workspace scythe \
    benchmark -l solidity -b smart2 -o output

  docker run --rm -it -v "$PWD:/workspace" -v /var/run/docker.sock:/var/run/docker.sock -w /workspace scythe \
    benchmark -l c -o output
EOF
}

prepare_scythe_environment() {
  local source_file=""
  local language=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --source-file)
        shift
        [[ $# -gt 0 ]] && source_file="$1"
        ;;
      --language|-l)
        shift
        [[ $# -gt 0 ]] && language="$1"
        ;;
      *)
        ;;
    esac
    shift || break
  done

  if [[ -z "$source_file" ]]; then
    return 0
  fi

  if [[ -z "$language" && "$source_file" == *.sol ]]; then
    language="solidity"
  fi

  if [[ "$language" != "solidity" ]]; then
    return 0
  fi

  local version_file
  version_file="$(cd "$(dirname "$source_file")" 2>/dev/null && pwd)/version"
  if [[ ! -f "$version_file" ]]; then
    return 0
  fi

  if command -v solc-select >/dev/null 2>&1; then
    local version
    version="$(tr -d '\r\n' < "$version_file")"
    solc-select use "$version" >/dev/null 2>&1 || true
  fi
}

if [[ $# -eq 0 ]]; then
  exec bash "$ROOT_DIR/scripts/run-benchmarks.sh"
fi

case "$1" in
  help|-h|--help)
    usage
    exit 0
    ;;
  scythe)
    shift
    prepare_scythe_environment "$@"
    exec scythe "$@"
    ;;
  benchmark)
    shift
    exec bash "$ROOT_DIR/scripts/run-benchmarks.sh" "$@"
    ;;
  *)
    if [[ "$1" == --source-file || "$1" == --script || "$1" == --language || "$1" == -l || "$1" == --benchmark || "$1" == -b || "$1" == --output || "$1" == -o || "$1" == --only-perses || "$1" == --only-scythe ]]; then
      prepare_scythe_environment "$@"
      exec scythe "$@"
    fi

    exec bash "$ROOT_DIR/scripts/run-benchmarks.sh" "$@"
    ;;
 esac
