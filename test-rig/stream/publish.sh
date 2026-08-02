#!/bin/sh
set -eu

STREAM_URL="rtsp://${MEDIAMTX_HOST:-mediamtx}:8554/${STREAM_PATH:-backyard}"
READY_MARKER=/tmp/streaming_ready

while true; do
  rm -f "$READY_MARKER"
  echo "publishing test dataset to $STREAM_URL"
  # AAC, not raw PCM: mediamtx rejects a raw-PCM RTSP ANNOUNCE with
  # "400 Bad Request" (confirmed by actually running this). AAC/RTP is
  # broadly supported and also matches what real IP cameras send -
  # bark-detector's own ffmpeg pull decodes back to raw PCM regardless.
  ffmpeg -nostdin -re -stream_loop -1 -f concat -safe 0 -i /data/playlist.txt \
    -ar 16000 -ac 1 -c:a aac -b:a 64k \
    -f rtsp -rtsp_transport tcp "$STREAM_URL" &
  FFMPEG_PID=$!

  # a failed RTSP ANNOUNCE (connection refused, bad request, etc) fails
  # fast - if ffmpeg is still alive a few seconds in, it connected. This is
  # what docker-compose's healthcheck polls for (see healthcheck.sh),
  # rather than querying mediamtx's own API, which requires auth for
  # anything but localhost by default and isn't worth fighting here.
  sleep 3
  if kill -0 "$FFMPEG_PID" 2>/dev/null; then
    touch "$READY_MARKER"
  fi

  wait "$FFMPEG_PID" || true
  rm -f "$READY_MARKER"
  echo "publisher exited (mediamtx not ready yet, or stream dropped), retrying in 2s"
  sleep 2
done
