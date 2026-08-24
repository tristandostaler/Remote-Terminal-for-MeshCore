# Stage 1: Build frontend
FROM node:24-slim AS frontend-builder

ARG COMMIT_HASH=unknown

WORKDIR /build

COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
RUN npm ci

COPY frontend/ ./
RUN VITE_COMMIT_HASH=${COMMIT_HASH} npm run build


# Stage 2: Python runtime
FROM python:3.14-slim

ARG COMMIT_HASH=unknown

WORKDIR /app

ENV COMMIT_HASH=${COMMIT_HASH}

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.6 /uv /usr/local/bin/uv

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./

# Optional AEIC neural image codec (onnxruntime + numpy + Pillow, ~120 MB
# installed), off by default so the standard image stays small.
#
# THE NORMAL WAY TO ENABLE IT IS AT RUNTIME, not here: set
# MESHCORE_ENABLE_AEIC=true and run.sh installs it on first start. That works
# with docker-compose, `docker run -e`, and the Home Assistant add-on options
# without a rebuild or a custom image.
#
# This build arg only PRE-BAKES it, for a deployment that would rather pay the
# cost at build time than on first start:
#
#   docker build --build-arg ENABLE_AEIC=1 -t remoteterm .
#
# Pre-baking makes the runtime step a no-op -- run.sh finds onnxruntime already
# present and skips straight to serving.
#
# 64-bit only either way: onnxruntime publishes manylinux wheels for x86_64 and
# aarch64 only, so this WILL fail to build on armv7/armhf/i386. Decoding a photo
# also needs ~2.4 GiB of RAM available to the container.
ARG ENABLE_AEIC=0

# Install dependencies (no dev/test deps)
RUN if [ "$ENABLE_AEIC" = "1" ]; then \
        uv sync --frozen --no-dev --extra aeic; \
    else \
        uv sync --frozen --no-dev; \
    fi

# Copy application code (remoteterm/ is the import surface for DB-stored bots)
COPY app/ ./app/
COPY remoteterm/ ./remoteterm/

# Copy license attributions
COPY LICENSES.md ./

# Copy built frontend from first stage
COPY --from=frontend-builder /build/dist ./frontend/dist

# Create data directory for SQLite database
RUN mkdir -p /app/data

RUN apt-get update && apt-get install -y --no-install-recommends jq libcodec2-1.2 \
    && rm -rf /var/lib/apt/lists/*

COPY run.sh ./
RUN chmod +x run.sh

EXPOSE 8000

# Run the application (we retain root for max compatibility)
CMD ["./run.sh"]
