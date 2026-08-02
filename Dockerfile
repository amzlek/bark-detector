# syntax=docker/dockerfile:1.6
#
# linux/amd64, size-optimized image:
#   - static ffmpeg binary (no apt codec/library tree)
#   - python:3.14-slim base
#   - ai-edge-litert (Google's official, actively maintained TFLite
#     interpreter) ships a prebuilt wheel on PyPI, so a plain pip install
#     resolves it automatically - no manual wheel URL to maintain
#   - no build toolchain in the final image (model fetched in an earlier
#     stage and copied in)
#
# linux/arm64 isn't published yet - see README's "Status / not yet done".
#
# Build with:
#   docker build -t bark-detector:latest .

FROM mwader/static-ffmpeg:7.1.1 AS ffmpeg

FROM python:3.14-slim AS model-fetch
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

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg
COPY --from=model-fetch /models/cpu_audio_model.tflite /app/cpu_audio_model.tflite
COPY audio-labelmap.txt /app/audio-labelmap.txt
COPY bark_detector /app/bark_detector

# safe to boot with nothing mounted at /config: a missing/empty config.yaml
# self-heals to defaults (zero sources, web UI on, MQTT off until you point
# it at a real broker) instead of failing - see bark_detector/config.py
ENV CONFIG_PATH=/config/config.yaml
VOLUME ["/config", "/media"]
EXPOSE 8099

ENTRYPOINT ["python", "-m", "bark_detector.main"]
