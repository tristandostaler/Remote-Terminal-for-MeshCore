#!/usr/bin/env bash
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$APP_DIR/.venv/bin/python"

OPTIONS=/data/options.json

if [ -f "$OPTIONS" ]; then
  # keys are already uppercase -> export as-is. Optional options left empty
  # (or cleared after being set) are skipped, so they mean "unset" and the
  # app's default applies, rather than exporting a literal "null" or "".
  eval "$(jq -r 'to_entries | .[] | select(.value != null and .value != "") | "export \(.key)=\(.value | @sh)"' "$OPTIONS")"
fi

# ── optional AEIC neural image codec ──────────────────────────────────────────
#
# Installed HERE, at start, rather than at image build, so it can be turned on
# with an environment variable from docker-compose, a plain `docker run -e`, or
# the Home Assistant add-on options — no rebuild and no custom image.
#
# This block only ever INSTALLS; it cannot uninstall. Turning the codec back off
# is therefore the app's job, not this script's: MESHCORE_ENABLE_AEIC is also
# read at runtime (app/config.py) and an explicit false switches the codec off
# even on a server where the dependency and the 958 MiB model are already
# present. Leaving it to this script alone meant flipping the value back to
# false changed nothing, because onnxruntime still imported.
#
# Deliberately non-fatal. This is an optional feature, and losing the radio
# because an image codec could not install would be a bad trade, so every failure
# path below warns and carries on; the app then shows the AI photo option as
# disabled, with the reason.
#
# Pre-baking with `docker build --build-arg ENABLE_AEIC=1` still works and makes
# this block a no-op, because the check below finds onnxruntime already present.

flag_on() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1 | true | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

aeic_requested() { flag_on "${MESHCORE_ENABLE_AEIC:-}"; }
llm_requested() { flag_on "${MESHCORE_ENABLE_LLM:-}"; }

# Probes the project venv directly rather than through `uv run`, so this works on
# any uv version without depending on a particular flag being available.
py_has() {
  [ -x "$VENV_PYTHON" ] && "$VENV_PYTHON" -c "import $1" >/dev/null 2>&1
}
aeic_installed() { py_has onnxruntime; }
llm_installed() { py_has llama_cpp; }

# `uv sync` REMOVES every extra it is not told about, so each sync below names
# every extra that is wanted *or already present* -- otherwise installing one
# optional feature would silently uninstall the other (or a pre-baked one).
extra_args() {
  local args=""
  if aeic_installed || { aeic_requested && [ "$AEIC_ARCH_OK" = 1 ]; }; then
    args="$args --extra aeic"
  fi
  if llm_installed || [ "${1:-}" = "with-llm" ]; then
    args="$args --extra llm"
  fi
  printf '%s' "$args"
}

# The data folder is wherever the database lives -- the same rule the app uses
# for its model folders (app/config.py) -- so moving the database with
# MESHCORE_DATABASE_PATH moves the caches below with it. Relative paths resolve
# against the app folder, as they do for the app.
abs_path() {
  case "$1" in /*) printf '%s' "$1" ;; *) printf '%s' "$APP_DIR/$1" ;; esac
}
DATA_DIR="$(dirname "$(abs_path "${MESHCORE_DATABASE_PATH:-data/meshcore.db}")")"

# Cache under the data folder so recreating the container does not re-download
# wheels -- or, for the LLM, recompile llama.cpp: uv caches the wheel it built.
# Harmless if that path is not writable: uv falls back to its default cache and
# only the speed-up is lost.
export UV_CACHE_DIR="$(abs_path "${UV_CACHE_DIR:-$DATA_DIR/.uv-cache}")"

case "$(uname -m)" in
  x86_64 | amd64 | aarch64 | arm64) AEIC_ARCH_OK=1 ;;
  *) AEIC_ARCH_OK=0 ;;
esac

if aeic_requested; then
  if aeic_installed; then
    echo "AEIC image codec: already installed."
  elif [ "$AEIC_ARCH_OK" = 1 ]; then
    echo "AEIC image codec: installing dependencies (~120 MB, first start only)..."
    # shellcheck disable=SC2046
    if uv sync --frozen --no-dev $(extra_args) && aeic_installed; then
      echo "AEIC image codec: dependencies ready."
      echo "AEIC image codec: the ~958 MB model is a separate one-time download —"
      echo "  use a conversation's features panel or POST /api/aeic/model/download."
    else
      echo "WARNING: AEIC image codec dependencies failed to install." >&2
      echo "         Starting without it; the AI photo option stays disabled." >&2
    fi
  else
    # onnxruntime publishes manylinux wheels for x86_64 and aarch64 only.
    echo "WARNING: MESHCORE_ENABLE_AEIC is set but this is $(uname -m), which onnxruntime" >&2
    echo "         publishes no wheels for (x86_64 or aarch64 required)." >&2
    echo "         Starting without the AI image codec." >&2
  fi
fi

# ── optional tiny LLM for the `tinyllm` bot ───────────────────────────────────────
#
# llama-cpp-python ships as source only, so the first install COMPILES llama.cpp:
# a few minutes on a desktop, up to an hour on a small Pi (memory limits it to
# one compile job there). That must never keep the radio offline,
# so unlike the codec above it runs in the BACKGROUND, after the server is up.
# The app imports llama_cpp lazily, the first time the bot loads a model, so it
# picks the package up without a restart; until then the bot answers that it is
# still installing (the marker files below are what it reads).
#
# Compilers are fetched with apt only when the build needs them -- a container
# recreated later reinstalls from the wheel cached in UV_CACHE_DIR in seconds --
# and removed again afterwards. llama.cpp is built for THIS machine's CPU (its
# default), which is what makes it fast; the cached wheel is tied to that CPU.
#
# Same non-fatal posture: any failure is logged and the server keeps running.

LLM_DIR="$(abs_path "${MESHCORE_LLM_MODEL_DIR:-$DATA_DIR/models/llm}")"

llm_background_install() {
  local log="$LLM_DIR/.install.log"
  # Wait for the server: `uv run` below syncs the venv at launch and takes the
  # same environment lock this sync needs, so racing it would stall startup.
  for _ in $(seq 1 120); do
    "$VENV_PYTHON" -c "import socket; socket.create_connection(('127.0.0.1', 8000), 1)" \
      >/dev/null 2>&1 && break
    sleep 2
  done
  echo "LLM (tinyllm bot): installing llama-cpp-python in the background..."
  local built_tools=0
  # Compiling llama.cpp is the heaviest thing this server ever does: measured
  # at up to ~700 MB per compiler process, 1.8 GB with four in parallel, which
  # is enough to take a 1-2 GB Pi down with the radio server on it. Allow one
  # parallel job per ~800 MB currently available (at least 1, at most one per
  # core), and run the build at the lowest CPU and I/O priority.
  local avail_mb jobs
  avail_mb=$(awk '/^MemAvailable:/ {print int($2 / 1024)}' /proc/meminfo 2>/dev/null)
  jobs=$(( ${avail_mb:-0} / 800 ))
  [ "$jobs" -lt 1 ] && jobs=1
  [ "$jobs" -gt "$(nproc)" ] && jobs=$(nproc)
  export CMAKE_BUILD_PARALLEL_LEVEL="${CMAKE_BUILD_PARALLEL_LEVEL:-$jobs}"
  if [ "${avail_mb:-0}" -gt 0 ] && [ "$avail_mb" -lt 900 ]; then
    echo "WARNING: LLM (tinyllm bot): only ${avail_mb} MB free; compiling llama.cpp may" >&2
    echo "         need ~700 MB and will be slow, or be killed by the OOM killer." >&2
  fi
  local lowprio="nice -n 19"
  command -v ionice >/dev/null 2>&1 && lowprio="$lowprio ionice -c 3"
  # --inexact: only ADD packages. The server is already running from this venv,
  # and a plain sync would also strip whatever `uv run` added at launch.
  # shellcheck disable=SC2046,SC2086
  if ! $lowprio uv sync --frozen --no-dev --inexact $(extra_args with-llm) >"$log" 2>&1; then
    if ! command -v cmake >/dev/null 2>&1 || ! command -v g++ >/dev/null 2>&1; then
      echo "LLM (tinyllm bot): fetching compilers to build llama.cpp (first time only)..."
      if apt-get update >>"$log" 2>&1 \
        && apt-get install -y --no-install-recommends build-essential cmake >>"$log" 2>&1; then
        built_tools=1
      fi
    fi
    echo "LLM (tinyllm bot): compiling llama.cpp with ${CMAKE_BUILD_PARALLEL_LEVEL} job(s) — a few minutes on a desktop, up to an hour on a small Pi..."
    # shellcheck disable=SC2046,SC2086
    $lowprio uv sync --frozen --no-dev --inexact $(extra_args with-llm) >>"$log" 2>&1 || true
  fi
  if [ "$built_tools" = 1 ]; then
    apt-get purge -y --auto-remove build-essential cmake >/dev/null 2>&1 || true
    rm -rf /var/lib/apt/lists/*
  fi
  rm -f "$LLM_DIR/.installing"
  if llm_installed; then
    rm -f "$LLM_DIR/.install-failed"
    echo "LLM (tinyllm bot): llama-cpp-python ready. Enable the tinyllm bot under Bots."
  else
    touch "$LLM_DIR/.install-failed"
    echo "WARNING: LLM (tinyllm bot): llama-cpp-python failed to install; see $log" >&2
  fi
}

if llm_requested; then
  if llm_installed; then
    echo "LLM (tinyllm bot): llama-cpp-python already installed."
  elif mkdir -p "$LLM_DIR" 2>/dev/null; then
    touch "$LLM_DIR/.installing"
    rm -f "$LLM_DIR/.install-failed"
    llm_background_install &
  else
    echo "WARNING: LLM (tinyllm bot): cannot create $LLM_DIR; skipping install." >&2
  fi
fi

exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
