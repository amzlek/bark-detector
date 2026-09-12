# syntax=docker/dockerfile:1.6
#
# Publishes a labeled bark/not-bark test set as a live RTSP stream to the
# mediamtx service, so bark-detector can consume it via a plain `type: rtsp`
# source exactly like a real camera's audio track.
#
# The dataset itself is fetched at container startup, not baked into the
# image: entrypoint.sh runs download_audioset.py against /data, which
# docker-compose.yml binds to ./dataset on the host - so it downloads once
# and is reused (and inspectable) across restarts, without polluting the
# image with fetched-on-demand, non-redistributable YouTube audio.

FROM mwader/static-ffmpeg:7.1.1 AS ffmpeg

FROM python:3.14-slim
COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg /ffprobe /usr/local/bin/ffprobe

WORKDIR /app
COPY pyproject.toml /app/pyproject.toml
RUN pip install --no-cache-dir .

COPY stream/download_audioset.py /app/download_audioset.py
COPY stream/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY stream/publish.sh /usr/local/bin/publish.sh
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/publish.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
