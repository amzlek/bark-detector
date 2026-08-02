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
  - `/` - event log with an inline player per detection, plus a live status
    panel (websocket connection, MQTT broker, per-source connectivity)
  - `/settings` - add/edit/delete sources (with a live connectivity badge
    and a "Test" button that probes a path before you save it), edit MQTT
    broker settings, and a small storage panel (recordings / space used /
    space available)
  - live status and all settings CRUD (add/edit/delete source, update MQTT)
    go over a websocket at `/ws`, gated by an auth token the app generates
    itself and self-heals into `config.yaml` on first boot (see
    `config.example.yaml`). Everything else - `/api/events`, `/api/sources`
    (read-only), `/api/stats`, `/snippets/*` - stays plain, unauthenticated
    HTTP
- **Automatic retention**: age- and space-based cleanup of saved snippets,
  checked on an interval.
- **Minimal image**: `python:3.14-slim` + a static ffmpeg binary (no apt
  codec tree) + [ai-edge-litert](https://pypi.org/project/ai-edge-litert/)
  (Google's official TFLite runtime, prebuilt wheel) - no TensorFlow, no
  Node/React build stage. `linux/amd64` only for now (see "Status / not yet
  done" below).

## Quick start

```bash
docker compose up --build
```

That's it - no config file required first. Defaults are: zero sources, web
UI on, MQTT off. Open the settings UI and add a source; MQTT stays off
until you turn it on there (or in config.yaml, or `MQTT_ENABLED=true`).

- Event log / settings UI: http://localhost:8099
- Saved snippets and the event log db land in `./snippets` (bind-mounted)
- Config lives at `./config/config.yaml` (bind-mounted as a directory, not
  a single file - see the comment in `docker-compose.yml` for why). A
  missing/empty/partial file self-heals into a fully-populated one with
  defaults on first boot, and the settings UI keeps it updated after that
  whenever you add/edit/delete a source or change MQTT settings.

Prefer to hand-configure things upfront instead? Copy `config.example.yaml`
to `./config/config.yaml` before starting - every field is documented
there, including which env var overrides it. Top-level sections: `mqtt`,
`sources`, `web`, `cleanup`, plus `snippet_dir`, `ffmpeg_path`, `log_level`.

### MQTT topics

MQTT is disabled by default (`mqtt.enabled: false` / `MQTT_ENABLED=false`).
Topics are all derived from a single configurable prefix, `mqtt.topic`
(default `bark_detector`):

| Topic (with the default prefix)       | When                                  | Retained |
|----------------------------------------|----------------------------------------|:--------:|
| `bark_detector/{source}/event`         | a detection fires                      | no       |
| `bark_detector/{source}/status`        | a source connects/disconnects          | yes      |
| `bark_detector/system/{event}`         | e.g. `space_limit_reached` on cleanup  | no       |

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
  templates/               event log + settings pages (plain HTML/JS, no build step)
  static/                  shared stylesheet + websocket client used by both pages
config.example.yaml      annotated config reference - optional, copy to ./config/config.yaml
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
- `linux/arm64` image - removed for now; this release only builds/publishes
  `linux/amd64`. Revisit once there's real ARM hardware to verify against.
- No CI yet - planned: a GitHub Actions workflow that at minimum imports
  the package and builds the Docker image on every push/PR, so a broken
  build/import is caught before release rather than at `docker compose up`.

## License

[MIT](LICENSE)

## AI assistance

Large parts of this project (implementation and documentation) were built
with AI assistance (Claude Code).
