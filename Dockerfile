# syntax=docker/dockerfile:1.6
#
# linux/amd64 and linux/arm64, size-optimized image:
#   - static ffmpeg binary (no apt codec/library tree)
#   - python:3.14-slim base
#   - ai-edge-litert (Google's official, actively maintained TFLite
#     interpreter) ships a prebuilt wheel on PyPI, so a plain pip install
#     resolves it automatically - no manual wheel URL to maintain
#   - no build toolchain in the final image (model fetched in an earlier
#     stage and copied in)
#
# Build with:
#   docker build -t bark-detector:latest .

FROM mwader/static-ffmpeg:7.1.1 AS ffmpeg

# The model file is architecture-independent, so fetch it on the builder's
# native platform instead of emulating apt/wget for every target architecture.
FROM --platform=$BUILDPLATFORM python:3.14-slim AS model-fetch
RUN apt-get update \
    && apt-get install -y --no-install-recommends wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /models
RUN wget -qO - https://www.kaggle.com/api/v1/models/google/yamnet/tfLite/classification-tflite/1/download \
    | tar xz && mv 1.tflite cpu_audio_model.tflite

# must be glibc-based (not e.g. python:3.14-alpine): ai-edge-litert is a
# compiled C++ project distributed only as prebuilt manylinux wheels, no
# musllinux wheel and no sdist either - `pip install` on Alpine finds zero
# matching distributions, confirmed by actually trying it, not just guessed
FROM python:3.14-slim AS base
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE /app/
COPY bark_detector /app/bark_detector
RUN pip install --no-cache-dir --no-compile .

COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg
COPY --from=model-fetch /models/cpu_audio_model.tflite /app/cpu_audio_model.tflite
COPY audio-labelmap.txt /app/audio-labelmap.txt

# safe to boot with nothing mounted at /config: a missing/empty config.yaml
# or sources dir runs on defaults (zero sources, web UI on, MQTT off until
# you point it at a real broker) instead of failing - see
# bark_detector/config.py. CONFIG_PATH (app settings: mqtt/web/cleanup/etc)
# and SOURCES_DIR (one *.yaml per source) are separate so CONFIG_PATH can
# be skipped entirely for a deployment that sets everything via env vars.
ENV CONFIG_PATH=/config/config.yaml
ENV SOURCES_DIR=/config/sources
VOLUME ["/config", "/media"]
EXPOSE 8099

ENTRYPOINT ["python", "-m", "bark_detector.main"]
