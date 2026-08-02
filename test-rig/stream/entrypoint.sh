#!/bin/sh
set -eu

DATA_DIR=/data

FORCE_ARGS=""
if [ "${FORCE_REFETCH:-false}" = "true" ]; then
  FORCE_ARGS="--force"
fi

# fetch the test dataset if not already present. /data is bind-mounted from
# the host (see docker-compose.yml), so this only actually downloads once -
# subsequent `docker compose up` runs just reuse what's there.
python3 /app/download_audioset.py \
  --out-dir "$DATA_DIR" \
  --cache-dir "$DATA_DIR/.audioset_cache" \
  --bark "${BARK_COUNT:-5}" \
  --negative "${NEGATIVE_COUNT:-5}" \
  $FORCE_ARGS

# build a silence clip + an ffconcat playlist that alternates
# 01_bark.wav, silence, 02_negative.wav, silence, ... so consecutive clips
# in the dataset don't bleed into the same detection window. The
# [0-9][0-9]_*.wav glob deliberately excludes silence.wav itself.
ffmpeg -y -f lavfi -i anullsrc=channel_layout=mono:sample_rate=16000 -t 1 \
  -c:a pcm_s16le "$DATA_DIR/silence.wav"

: > "$DATA_DIR/playlist.txt"
: > "$DATA_DIR/durations.txt"
for f in "$DATA_DIR"/[0-9][0-9]_*.wav; do
  printf "file '%s'\n" "$f" >> "$DATA_DIR/playlist.txt"
  printf "file '%s'\n" "$DATA_DIR/silence.wav" >> "$DATA_DIR/playlist.txt"

  duration=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f")
  printf "%s %s\n" "$(basename "$f")" "$duration" >> "$DATA_DIR/durations.txt"
done

# background ticker: independently loops the same sequence forever (same
# order publish.sh's ffmpeg -stream_loop -1 plays), printing which clip
# should be playing right now - for matching bark-detector's log output
# against ground truth (the NN_bark.wav / NN_negative.wav filenames) while
# troubleshooting. Approximate, not frame-accurate: it's a separate timer
# racing the real playback, not driven by ffmpeg's actual position.
(
  while true; do
    while IFS=' ' read -r name duration; do
      echo "[stream] now playing: $name"
      sleep "$duration"
      sleep 1  # the silence gap between clips
    done < "$DATA_DIR/durations.txt"
  done
) &

exec /usr/local/bin/publish.sh
