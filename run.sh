#!/usr/bin/env bash
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$APP_DIR/.venv/bin/python"

OPTIONS=/data/options.json

if [ -f "$OPTIONS" ]; then
  # keys are already uppercase -> export as-is
  eval "$(jq -r 'to_entries | .[] | "export \(.key)=\(.value | @sh)"' "$OPTIONS")"
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

aeic_requested() {
  case "$(printf '%s' "${MESHCORE_ENABLE_AEIC:-}" | tr '[:upper:]' '[:lower:]')" in
    1 | true | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

# Probes the project venv directly rather than through `uv run`, so this works on
# any uv version without depending on a particular flag being available.
aeic_installed() {
  [ -x "$VENV_PYTHON" ] && "$VENV_PYTHON" -c "import onnxruntime" >/dev/null 2>&1
}

if aeic_requested; then
  if aeic_installed; then
    echo "AEIC image codec: already installed."
  else
    ARCH="$(uname -m)"
    case "$ARCH" in
      x86_64 | amd64 | aarch64 | arm64)
        # Cache under the data volume so recreating the container does not
        # re-download ~120 MB of wheels. Harmless if that path is not writable:
        # uv falls back to its default cache and only the speed-up is lost.
        export UV_CACHE_DIR="${UV_CACHE_DIR:-$APP_DIR/data/.uv-cache}"
        echo "AEIC image codec: installing dependencies (~120 MB, first start only)..."
        if uv sync --frozen --no-dev --extra aeic && aeic_installed; then
          echo "AEIC image codec: dependencies ready."
          echo "AEIC image codec: the ~958 MB model is a separate one-time download —"
          echo "  use a conversation's features panel or POST /api/aeic/model/download."
        else
          echo "WARNING: AEIC image codec dependencies failed to install." >&2
          echo "         Starting without it; the AI photo option stays disabled." >&2
        fi
        ;;
      *)
        # onnxruntime publishes manylinux wheels for x86_64 and aarch64 only.
        echo "WARNING: MESHCORE_ENABLE_AEIC is set but this is ${ARCH}, which onnxruntime" >&2
        echo "         publishes no wheels for (x86_64 or aarch64 required)." >&2
        echo "         Starting without the AI image codec." >&2
        ;;
    esac
  fi
fi

exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
