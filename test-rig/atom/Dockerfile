# syntax=docker/dockerfile:1.6
#
# Serves the same labeled test dataset as stream-publisher (see
# ../stream/Dockerfile), but as raw PCM over a listening TCP socket instead
# of RTSP - so bark-detector's `esphome_tcp` source type (see
# ../../esphome/tcp_audio_server.h) can be exercised end-to-end without real
# M5 Atom Echo hardware.
#
# Reuses stream-publisher's already-built playlist.txt/silence.wav from the
# shared ./dataset bind mount instead of re-fetching/re-building them - see
# entrypoint.sh - so no python/pandas/yt-dlp is needed here, just ffmpeg.

FROM mwader/static-ffmpeg:7.1.1 AS ffmpeg

FROM debian:bookworm-slim
COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg

COPY atom/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
