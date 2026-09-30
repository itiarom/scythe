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

  # For C benchmarks, either mount the host Docker socket or run with Docker-in-Docker support.
  docker run --rm -it --privileged \
    -v "$PWD:$PWD" \
    -w "$PWD" \
    scythe benchmark -l c -o output

  # The same absolute workspace path lets the host Docker daemon resolve nested bind mounts.
  docker run --rm -it \
    -v "$PWD:$PWD" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -w "$PWD" \
    scythe benchmark -l c -o output
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

has_c_language() {
  local arg
  for arg in "$@"; do
    if [[ "$arg" == "-l" || "$arg" == "--language" ]]; then
      continue
    fi
    if [[ "$arg" == "c" ]]; then
      return 0
    fi
  done
  return 1
}

ensure_docker_for_c() {
  local args=("$@")
  local arg

  if [[ ${#args[@]} -eq 0 ]]; then
    return 0
  fi

  local language=""
  for ((i=0; i<${#args[@]}; i++)); do
    arg="${args[$i]}"
    if [[ "$arg" == "-l" || "$arg" == "--language" ]]; then
      if (( i + 1 < ${#args[@]} )); then
        language="${args[$((i + 1))]}"
      fi
      break
    fi
  done

  if [[ "$language" != "c" ]]; then
    return 0
  fi

  if [[ ! -S /var/run/docker.sock ]]; then
    echo "Error: C benchmarks require the host Docker socket to be mounted at /var/run/docker.sock." >&2
    echo "Run the container with: -v /var/run/docker.sock:/var/run/docker.sock" >&2
    return 1
  fi

  export DOCKER_HOST="${DOCKER_HOST:-unix:///var/run/docker.sock}"

  if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "Error: Docker is unavailable inside the container. Mount /var/run/docker.sock from the host." >&2
    return 1
  fi

  return 0
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
    ensure_docker_for_c "$@"
    exec scythe "$@"
    ;;
  benchmark)
    shift
    ensure_docker_for_c "$@"
    exec bash "$ROOT_DIR/scripts/run-benchmarks.sh" "$@"
    ;;
  *)
    if [[ "$1" == --source-file || "$1" == --script || "$1" == --language || "$1" == -l || "$1" == --benchmark || "$1" == -b || "$1" == --output || "$1" == -o || "$1" == --only-perses || "$1" == --only-scythe ]]; then
      prepare_scythe_environment "$@"
      ensure_docker_for_c "$@"
      exec scythe "$@"
    fi

    ensure_docker_for_c "$@"
    exec bash "$ROOT_DIR/scripts/run-benchmarks.sh" "$@"
    ;;
 esac
