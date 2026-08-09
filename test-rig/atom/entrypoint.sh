#!/bin/sh
set -eu

DATA_DIR=/data
PORT="${STREAM_PORT:-12345}"
READY_MARKER=/tmp/streaming_ready

while true; do
  rm -f "$READY_MARKER"
  echo "serving raw PCM on tcp://0.0.0.0:$PORT (emulates esphome/tcp_audio_server.h)"
  # "?listen" makes ffmpeg's tcp muxer act as a server, like the real Atom
  # Echo firmware - bark-detector connects OUT to it as a client, same as
  # `ffplay -f s16le -ar 16000 -ac 1 -i tcp://<ip>:<port>` in
  # esphome/test_stream.py. No container/framing on the wire: raw s16le
  # mono PCM, matching tcp_audio_server.h exactly.
  ffmpeg -nostdin -re -stream_loop -1 -f concat -safe 0 -i "$DATA_DIR/playlist.txt" \
    -ar 16000 -ac 1 -f s16le "tcp://0.0.0.0:${PORT}?listen=1" &
  FFMPEG_PID=$!

  # unlike an RTSP ANNOUNCE, binding a listen socket doesn't fail just
  # because no client has connected yet - this only catches ffmpeg failing
  # to start at all (e.g. playlist.txt missing, port already in use)
  sleep 3
  if kill -0 "$FFMPEG_PID" 2>/dev/null; then
    touch "$READY_MARKER"
  fi

  wait "$FFMPEG_PID" || true
  rm -f "$READY_MARKER"
  echo "ffmpeg exited (client disconnected, or another reason), restarting in 2s"
  sleep 2
done
