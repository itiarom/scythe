#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRY_RUN=false
LANGUAGE="all"
BENCHMARK=""

usage() {
  cat <<'EOF'
Usage:
  ./scripts/docker-setup.sh [OPTIONS]

Provision the compiler/toolchain dependencies for the benchmark examples.

Options:
  -l, --language LANGUAGE   Toolchain to install: all, solidity, java, c, compcert
  -b, --benchmark NAME     Install only a specific benchmark or example name
      --dry-run            Print the actions without changing the system
  -h, --help               Show this help message

Examples:
  ./scripts/docker-setup.sh --language solidity
  ./scripts/docker-setup.sh --language c --benchmark clang-23309
  ./scripts/docker-setup.sh --language all --dry-run
EOF
}

run_cmd() {
  if $DRY_RUN; then
    echo "$*"
  else
    "$@"
  fi
}

ensure_sdkman() {
  if [[ -f "$HOME/.sdkman/bin/sdkman-init.sh" ]]; then
    return 0
  fi

  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required to install SDKMAN." >&2
    return 1
  fi

  echo "Installing SDKMAN into $HOME/.sdkman"
  run_cmd bash -lc 'curl -s "https://get.sdkman.io" | bash'

  if [[ -f "$HOME/.sdkman/bin/sdkman-init.sh" ]]; then
    return 0
  fi

  echo "SDKMAN installation did not create the expected init script." >&2
  return 1
}

source_sdkman() {
  if [[ -f "$HOME/.sdkman/bin/sdkman-init.sh" ]]; then
    export SDKMAN_DIR="${SDKMAN_DIR:-$HOME/.sdkman}"
    export SDKMAN_CANDIDATES_API="${SDKMAN_CANDIDATES_API:-}"

    # SDKMAN's init script is not safe under `set -u` until its environment
    # variables are initialized, so temporarily disable nounset while sourcing it.
    set +u
    # shellcheck disable=SC1090
    source "$HOME/.sdkman/bin/sdkman-init.sh"
    set -u
  fi
}

install_solidity_versions() {
  if ! command -v solc-select >/dev/null 2>&1; then
    echo "Installing solc-select"
    run_cmd python3 -m pip install solc-select
  fi

  local versions=()
  if [[ -n "$BENCHMARK" ]]; then
    if [[ -f "$ROOT_DIR/Solidity/$BENCHMARK/version" ]]; then
      versions+=("$(cat "$ROOT_DIR/Solidity/$BENCHMARK/version")")
    fi
  else
    while IFS= read -r f; do
      if [[ -f "$f/version" ]]; then
        versions+=("$(cat "$f/version")")
      fi
    done < <(find "$ROOT_DIR/Solidity" -mindepth 1 -maxdepth 1 -type d -name 'smart*' | sort)
  fi

  if [[ ${#versions[@]} -eq 0 ]]; then
    echo "No Solidity benchmark versions found under $ROOT_DIR/Solidity"
    return 0
  fi

  for v in "${versions[@]}"; do
    echo "Ensuring Solidity compiler $v is installed"
    if $DRY_RUN; then
      echo "solc-select install $v"
    else
      solc-select install "$v" >/dev/null 2>&1 || true
    fi
  done
}

resolve_sdk_java_id() {
  local major="$1"
  local init_file="$HOME/.sdkman/bin/sdkman-init.sh"
  local vendor use version identifier extra

  if [[ ! -f "$init_file" ]]; then
    return 1
  fi

  while IFS='|' read -r vendor use version identifier extra; do
    version="${version//[[:space:]]/}"
    identifier="${identifier//[[:space:]]/}"
    if [[ -n "$version" && "$version" == "$major".* && -n "$identifier" ]]; then
      printf '%s\n' "$identifier"
      return 0
    fi
  done < <(bash -lc "source '$init_file' >/dev/null 2>&1 || true; sdk list java 2>/dev/null")

  return 1
}

install_java_toolchains() {
  ensure_sdkman
  if ! $DRY_RUN; then
    source_sdkman
  fi

  local specs=("11" "17" "21")
  if [[ -n "$BENCHMARK" ]]; then
    if [[ -f "$ROOT_DIR/Java/$BENCHMARK/version" ]]; then
      specs=("$(cat "$ROOT_DIR/Java/$BENCHMARK/version")")
    fi
  fi

  for spec in "${specs[@]}"; do
    echo "Ensuring Java toolchain $spec is available"
    if $DRY_RUN; then
      echo "sdk install java $(resolve_sdk_java_id "$spec" || echo "$spec")"
    else
      source_sdkman
      local init_file="$HOME/.sdkman/bin/sdkman-init.sh"
      if [[ -f "$init_file" ]]; then
        local id
        id="$(resolve_sdk_java_id "$spec")"
        if [[ -n "$id" ]]; then
          # SDKMAN prompts to set the candidate as default when installing a new version.
          # For non-interactive CI/Docker contexts, answer "no" so the install does not hang.
          bash -lc "source '$init_file' >/dev/null 2>&1 || true; printf 'n\\n' | sdk install java '$id' >/dev/null 2>&1 || true"
        else
          echo "No Java $spec candidate was available via SDKMAN; skipping installation." >&2
        fi
      else
        echo "SDKMAN was not initialized correctly; skipping Java toolchain installation for $spec" >&2
      fi
    fi
  done
}

build_c_images() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required for the C benchmark toolchains; install Docker or mount the host socket."
    return 0
  fi

  if ! docker info >/dev/null 2>&1; then
    echo "Docker daemon is unavailable; skipping C benchmark image builds. Mount the host Docker socket or run this script on a Docker-enabled host."
    return 0
  fi

  local files=()
  if [[ -n "$BENCHMARK" ]]; then
    if [[ -d "$ROOT_DIR/C/$BENCHMARK" ]]; then
      files=("$ROOT_DIR/dockerfiles"/*.dockerfile)
    fi
  else
    files=("$ROOT_DIR/dockerfiles"/*.dockerfile)
  fi

  if [[ ${#files[@]} -eq 0 ]]; then
    echo "No C dockerfiles found under $ROOT_DIR/dockerfiles"
    return 0
  fi

  echo "Building C benchmark images from ${#files[@]} Dockerfiles"
  for dockerfile in "${files[@]}"; do
    local name tag major minor patch assertions
    name="$(basename "$dockerfile" .dockerfile)"
    if [[ "$name" =~ ^(clang|gcc)_([0-9]+)_([0-9]+)(_([0-9]+))?(_assertions)?$ ]]; then
      major="${BASH_REMATCH[2]}"
      minor="${BASH_REMATCH[3]}"
      patch="${BASH_REMATCH[5]}"
      assertions="${BASH_REMATCH[6]}"
      tag="${BASH_REMATCH[1]}-${major}.${minor}"
      [[ -n "$patch" ]] && tag+=".${patch}"
      [[ -n "$assertions" ]] && tag+="-assertions"
    else
      tag="${name//_/-}"
    fi
    echo "  -> $tag"
    if $DRY_RUN; then
      echo "docker build -t "$tag" --file "$dockerfile" "$ROOT_DIR""
    else
      docker build -t "$tag" --file "$dockerfile" "$ROOT_DIR" >/dev/null
    fi
  done
}

install_compcert() {
  if ! command -v opam >/dev/null 2>&1; then
    echo "opam is required for CompCert. Install opam and then rerun this setup step."
    return 0
  fi

  echo "Initialising opam and installing CompCert 3.7"
  if $DRY_RUN; then
    echo "opam init --disable-sandboxing -a --bare"
    echo "opam install -y coq-compcert.3.7~coq-platform coq.8.11.0"
    return 0
  fi

  export OPAMYES=1
  opam init --disable-sandboxing -a --bare >/dev/null 2>&1 || true
  eval "$(opam env --switch=default --set-switch)"
  opam install -y coq-compcert.3.7~coq-platform coq.8.11.0 >/dev/null 2>&1 || true
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -l|--language)
        LANGUAGE="${2:-all}"
        shift 2
        ;;
      -b|--benchmark)
        BENCHMARK="${2:-}"
        shift 2
        ;;
      --dry-run)
        DRY_RUN=true
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      --)
        shift
        break
        ;;
      *)
        echo "Unknown argument: $1" >&2
        usage >&2
        exit 1
        ;;
    esac
  done
}

main() {
  parse_args "$@"

  case "$LANGUAGE" in
    all)
      install_solidity_versions
      install_java_toolchains
      build_c_images
      install_compcert
      ;;
    solidity)
      install_solidity_versions
      ;;
    java)
      install_java_toolchains
      ;;
    c)
      build_c_images
      ;;
    compcert)
      install_compcert
      ;;
    *)
      echo "Unsupported language '$LANGUAGE'. Must be one of: all, solidity, java, c, compcert." >&2
      exit 1
      ;;
  esac
}

main "$@"
