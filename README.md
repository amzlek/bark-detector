# bark-detector

A small, lightweight service that listens to one or more audio streams,
detects a dog bark (or other configured sound), saves a WAV snippet around
the detection, and publishes it to MQTT. No video, no recording, no
database beyond a small local event log - built to be cheap to run in size,
memory, and CPU.

The classifier is a direct port of the audio-detection piece of
[Frigate NVR](https://github.com/blakeblackshear/frigate) (same YAMNet-derived
TFLite model and label set), extracted into a standalone service with no
dependency on Frigate's video/recording/app framework.

## Features

- **Pluggable audio sources**: an RTSP camera's audio track, or a USB/ALSA
  microphone. (A Wyoming-protocol source, e.g. for an M5 Atom Echo, is
  planned but not implemented yet.)
- **Configurable detection** per source: which labels to listen for,
  per-label thresholds, minimum volume gate, and pre/post-capture window for
  the saved snippet.
- **MQTT**:
  - a message per detection (label, score, timestamp, snippet path)
  - a retained connect/disconnect status message per source (based on audio
    actually flowing, not just the capture process being alive)
  - a notification when storage cleanup has to evict snippets to stay under
    its space cap
- **Web UI** (Flask, no separate frontend build step):
  - `/` - event log with an inline player per detection
  - `/settings` - add/edit/delete sources (with a live connectivity badge
    and a "Test" button that probes a path before you save it), edit MQTT
    broker settings, and a small storage panel (recordings / space used /
    space available)
- **Automatic retention**: age- and space-based cleanup of saved snippets,
  checked on an interval.
- **Multi-arch, minimal image**: `python:3.14-slim` + a static ffmpeg binary
  (no apt codec tree) + [ai-edge-litert](https://pypi.org/project/ai-edge-litert/)
  (Google's official TFLite runtime, prebuilt wheels for both
  `linux/amd64` and `linux/arm64`) - no TensorFlow, no Node/React build
  stage.

## Quick start

```bash
cp config.example.yaml config.yaml
# edit config.yaml: at least mqtt.host and your source(s)

docker compose up --build
```

- Event log / settings UI: http://localhost:8099
- Saved snippets and the event log db land in `./snippets` (bind-mounted)
- `config.yaml` is *not* read-only: the settings UI writes changes back to
  it, atomically, whenever you add/edit/delete a source or update MQTT
  settings

See `config.example.yaml` for every field, with comments. The top-level
sections are: `mqtt`, `sources`, `web`, `cleanup`, plus `snippet_dir`,
`ffmpeg_path`, `log_level`.

### MQTT topics

| Topic (default template)              | When                                  | Retained |
|----------------------------------------|----------------------------------------|:--------:|
| `bark_detector/{source}/event`         | a detection fires                      | no       |
| `bark_detector/{source}/status`        | a source connects/disconnects          | yes      |
| `bark_detector/system/{event}`         | e.g. `space_limit_reached` on cleanup  | no       |

Templates are configurable under `mqtt.topic` / `mqtt.status_topic` /
`mqtt.system_topic`.

## Multi-arch build

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t bark-detector:latest --push .
```

Only `linux/amd64` has actually been built and run end-to-end so far (see
`test-rig/`); the `linux/arm64` image builds against the same multi-arch
`ai-edge-litert`/ffmpeg base but hasn't been verified on real ARM hardware
yet.

## Local test rig

`test-rig/` spins up a full, real (not mocked) pipeline: a Mosquitto
broker, a mediamtx RTSP server, a small publisher that loops a labeled
bark/not-bark dataset into it as a live audio stream, and `bark-detector`
pulling that stream exactly like it would a real camera.

```bash
docker compose -f test-rig/docker-compose.yml up --build
```

No manual setup step needed - on first run, `stream-publisher` fetches a
small labeled test set from Google AudioSet (via `stream/download_audioset.py`,
see below) into the bind-mounted `test-rig/dataset/`, and reuses it on
every run after that. `bark-detector` waits for the stream to actually be
live before it starts (a Docker healthcheck on `stream-publisher`), so
there's no window of failed-connection retries at startup.

- Ground truth is in the filenames: `NN_bark.wav` / `NN_negative.wav`
- Watch detections live: `mosquitto_sub -h localhost -t 'bark_detector/#' -v`
- Event log / settings UI: http://localhost:8099
- `stream-publisher` logs `[stream] now playing: NN_bark.wav` as it cycles
  through the dataset, so you can line up bark-detector's log output
  against ground truth for troubleshooting

Optional env vars (set before `docker compose up`):

| Var | Default | |
|---|---|---|
| `BARK_COUNT` | `5` | number of bark samples to fetch |
| `NEGATIVE_COUNT` | `5` | number of non-bark samples to fetch |
| `FORCE_REFETCH` | `false` | re-download even if `dataset/` already has samples |

Positives are AudioSet's exact "Bark" label only; negatives exclude "Dog"
and every sound under it (bark, howl, growl, whimper, bay, ...) so a
negative clip is never a dog sound of any kind. AudioSet doesn't
redistribute audio itself (just YouTube id + timestamp + label), so the
fetched clips aren't committed - and inevitably, some AudioSet YouTube ids
are dead by now (removed/private videos); the script oversamples
candidates and skips failures until it hits the target count.

Want to fetch a bigger/different set without going through Docker? From
`test-rig/`:
`pip install -r requirements.txt && python stream/download_audioset.py --bark 15 --negative 15`
(needs `ffmpeg` on PATH).

## Project layout

```
bark_detector/           the application (Python package)
  static/                 event log + settings pages (plain HTML/JS, no build step)
config.example.yaml      annotated config reference - copy to config.yaml
requirements.txt
Dockerfile
docker-compose.yml       run against real hardware
test-rig/                local end-to-end test rig (mosquitto + mediamtx + publisher)
  dataset/                 fetched test clips - gitignored, not committed
  stream/                  the test publisher
    download_audioset.py     fetches the labeled test set from AudioSet (see above)
    entrypoint.sh             fetch-if-missing -> build playlist -> publish
```

## Status / not yet done

- Wyoming-protocol source (M5 Atom Echo and similar satellites) - stubbed,
  raises a clear error if configured, not implemented.
- `linux/arm64` image has not been built/booted on real hardware yet, only
  cross-checked for obvious blockers (see above).
