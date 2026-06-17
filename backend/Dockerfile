# syntax=docker/dockerfile:1
# connect backend — ONE image, two commands (runtime design §7):
#   api:    uvicorn connect.api.main:app ...
#   worker: python -m connect.workers.main
# One build, no drift. Multi-stage: builder makes the venv; runtime copies
# it, bakes the fastembed model, then layers the source LAST so code edits
# rebuild in seconds without re-downloading deps or the model.

# --- builder: dependency venv -------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY pyproject.toml ./
# Install ONLY [project].dependencies (not the package): this layer is
# cached until pyproject's dependency list changes, so source edits never
# re-download pymupdf/onnxruntime/etc. (no heredoc: stays buildable on the
# legacy builder as well as BuildKit)
RUN python -c "import tomllib; deps = tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']; open('/tmp/requirements.txt', 'w').write('\n'.join(deps) + '\n')" \
    && pip install -r /tmp/requirements.txt
# Optional reel-narration TTS (connect/social/tts.py). `|| true`: a flaky
# piper wheel must never fail the build — the runtime import is guarded and
# falls back to silent reels.
RUN pip install piper-tts || true

# --- runtime ------------------------------------------------------------------
FROM python:3.12-slim

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    # fastembed model cache — baked at BUILD time (below): cold containers
    # never download the model (slow, flaky, breaks offline). The model
    # version is pinned by the image digest. (~130MB; the trade-off the
    # design takes deliberately.)
    FASTEMBED_CACHE_PATH=/opt/fastembed \
    # default blob root inside containers; compose mounts the shared
    # `blobs` volume here for api (uploads) and workers (ingest)
    CONNECT_BLOB_DIR=/data/blobs \
    # baked Piper voice for reel narration (connect/social/tts.py); absent
    # voice => silent reels, so this is a quality bake, not a hard dep
    CONNECT_PIPER_VOICE_DIR=/opt/piper

# ffmpeg is required to assemble reel videos (slim image lacks it).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# Bake the embedding model (knowledge/embedder.DEFAULT_MODEL). Cached
# until the venv layer changes.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

# Bake the Piper voice model (~60MB). `|| true`: a flaky voice host must not
# fail the build — a cold container then just renders silent reels.
RUN python -m piper.download_voices en_US-lessac-medium --download-dir /opt/piper || true

# Non-root runtime user; owns the model cache (fastembed touches lock
# files next to the model) and the blob root (overlaid by the volume).
RUN useradd --create-home --uid 1000 connect \
    && chown -R connect:connect /opt/fastembed \
    && mkdir -p /opt/piper && chown -R connect:connect /opt/piper \
    && mkdir -p /data/blobs \
    && chown -R connect:connect /data

# Source last — the only layer that changes on a normal code edit.
WORKDIR /app
COPY pyproject.toml ./
COPY connect ./connect
RUN pip install --no-deps . && chown -R connect:connect /app

USER connect

EXPOSE 8000

# Default command = api; compose overrides per service (worker runs
# `python -m connect.workers.main`). --proxy-headers: Caddy terminates
# TLS, so X-Forwarded-Proto must be honored (Secure cookies, auth
# workstream); trust any in-compose proxy IP.
CMD ["uvicorn", "connect.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]
