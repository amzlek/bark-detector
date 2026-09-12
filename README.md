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

- **Pluggable audio sources**: an RTSP camera's audio track, a USB/ALSA
  microphone (untested), or an ESPHome device like an M5 Atom Echo streaming
  raw PCM over a bespoke TCP protocol (see `esphome/`).
- **Configurable detection** per source: which labels to listen for,
  per-label thresholds, minimum volume gate, and pre/post-capture window for
  the saved snippet.
- **MQTT**:
  - a message per detection (label, score, timestamp, snippet path)
  - a retained connect/disconnect status message per source (based on audio
    actually flowing, not just the capture process being alive)
  - a notification when storage cleanup has to evict snippets to stay under
    its space cap
- **Web UI** (Flask):
  - `/` - event log with an inline player per detection, plus a live status
    panel (websocket connection, MQTT broker, per-source connectivity)
  - `/settings` - add/edit/delete sources (with a live connectivity badge
    and a "Test" button that probes a path before you save it), edit MQTT
    broker settings, and a small storage panel (recordings / space used /
    space available)
  - live status and all settings CRUD (add/edit/delete source, update MQTT)
    go over an unauthenticated websocket at `/ws`. The HTTP endpoints are
    unauthenticated as well, so expose the UI only on a trusted network.
- **Automatic retention**: age- and space-based cleanup of saved snippets,
  checked on an interval.
- **Minimal image**: `python:3.14-slim` + a static ffmpeg binary (no apt
  codec tree) + [ai-edge-litert](https://pypi.org/project/ai-edge-litert/)
  (Google's official TFLite runtime, prebuilt wheel).
  `linux/amd64` only for now (see "Status / not yet done" below).

## Quick start

Build the image once from the repo root:

```bash
docker build -t bark-detector:latest .
```

Then pick one of two ready-to-run examples under `examples/` (both just
reference `image: bark-detector:latest` - neither builds it themselves):

- **`examples/basic_compose/`** - sources managed through the settings UI,
  a couple of env vars for MQTT. Start with:
  ```bash
  docker compose -f examples/basic_compose/docker-compose.yml up
  ```
- **`examples/no_ui_compose/`** - MQTT and sources fully specified as YAML
  files (`config/config.yaml` + `sources/*.yaml`), nothing via env vars,
  port 8099 not published. See `examples/config.default.yaml` for every
  app-config field spelled out (commented, at its default) as a starting
  template, and `examples/sources/` for the per-source file format.
  ```bash
  docker compose -f examples/no_ui_compose/docker-compose.yml up
  ```

Either way: zero sources and MQTT off is a valid starting state - open the
settings UI and add a source, MQTT stays off until you turn it on.

- Event log / settings UI: http://localhost:8099 (unless disabled)
- Saved snippets and the event log db land in the bind-mounted snippets volume
- Config is split between app settings and source files:
  - `config.yaml` (`CONFIG_PATH`, default `/config/config.yaml`) - app
    settings (`mqtt`, `web`, `cleanup`, `source_defaults`, `snippet_dir`,
    `ffmpeg_path`, `log_level`). Fully optional - every field can also come
    from an env var, so a pure-env-var deployment doesn't need this file
    mounted at all. The app never rewrites it just because it booted -
    only when the settings UI explicitly changes something (e.g. saving
    MQTT settings), and even then it only ever writes values that differ
    from their default *and* aren't currently sourced from an env var (env
    always wins regardless of what's on disk, so writing it back would
    just leak it into a plaintext file - this matters most for
    `mqtt.password`). If a field is set both in the file and via env var,
    the app logs a warning at startup naming both.
  - `sources/*.yaml` (`SOURCES_DIR`, default `/config/sources`) - one file
    per source, created/updated/deleted by the settings UI. This one does
    need somewhere to live, since sources aren't env-var configurable
    (there's no clean way to express a dynamic list of them that way).

### Optional Environment overrides
If the ENV is set, it will override and replace the value in the config.yaml

#### MQTT
| Var | Default | |
|---|---|---|
| `MQTT_ENABLED` | `false` | enable/disable MQTT client |
| `MQTT_HOST` | `localhost` | MQTT broker host |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USERNAME` | `None` | MQTT client username |
| `MQTT_PASSWORD` | `None` | MQTT client password |
| `MQTT_TOPIC` | `bark_detector` | MQTT message base topic |
| `MQTT_CLIENT_ID` | `bark_detector` | MQTT client ID |
| `MQTT_DISCOVERY` | `true` | publish Home Assistant MQTT discovery configs (see below) |
| `MQTT_DISCOVERY_PREFIX` | `homeassistant` | discovery topic prefix - match your HA `mqtt: discovery_prefix:` if you changed it from the default |


#### WEB UI
| Var | Default | |
|---|---|---|
| `WEB_ENABLED` | `true` | enable/disable Web UI |
| `WEB_HOST` | `0.0.0.0` | Web UI host |
| `WEB_PORT` | `8099` | Web UI port |


#### CLEANUP
| Var | Default | |
|---|---|---|
| `CLEANUP_MAX_AGE_DAYS` | `30` | delete snippet after X days |
| `CLEANUP_MAX_SPACE_MB` | `100` | trim snippets to remain below X MB  |
| `CLEANUP_CHECK_INTERVAL_MINUTES` | `15` | run trimming every X minutes |


#### OTHERS
| Var | Default | |
|---|---|---|
| `CONFIG_PATH` | `/config/config.yaml` | app config file (see above) |
| `SOURCES_DIR` | `/config/sources` | directory holding one *.yaml file per source |
| `SNIPPET_DIR` | `/media/bark_snippets` | folder to save snippets |
| `LOG_LEVEL` | `INFO` | log level (INFO/WARN/ERROR), parsed at startup|


### MQTT topics

MQTT is disabled by default (`mqtt.enabled: false` / `MQTT_ENABLED=false`).
Topics are all derived from a single configurable prefix, `mqtt.topic`
(default `bark_detector`):

| Topic (with the default prefix)       | When                                  | Retained |
|----------------------------------------|----------------------------------------|:--------:|
| `bark_detector/{source}/triggered`     | a bark is detected, before the snippet finishes recording | no |
| `bark_detector/{source}/event`         | the snippet finishes recording         | no       |
| `bark_detector/{source}/status`        | a source connects/disconnects          | yes      |
| `bark_detector/system/{event}`         | e.g. `space_limit_reached` on cleanup  | no       |

A detection produces **two** messages, seconds apart, sharing the same
`id` so a subscriber can correlate them - `triggered` fires immediately
(for automations that need to react fast, before any audio has been
recorded), `event` follows once the snippet is actually saved to disk:

```jsonc
// bark_detector/backyard/triggered - fired immediately
{"id": "3f9a...c2", "source": "backyard", "label": "bark", "score": 0.85, "timestamp": 1734000000.12}

// bark_detector/backyard/event - fired ~post_capture seconds later
{"id": "3f9a...c2", "source": "backyard", "label": "bark", "score": 0.91, "timestamp": 1734000000.12, "file": "/media/bark_snippets/backyard_bark_1734000000.wav", "duration": 10.0}
```

Note `score` can differ between the two: `triggered` reports the score at
the moment of detection, while `event`'s score is the max seen across the
whole capture (the bark may have gotten louder/clearer as it continued).

Whenever MQTT reconnects (including the very first connect), bark-detector
also publishes `bark_detector/bridge/status` (retained) as `online`, backed
by an MQTT Last Will so it flips to `offline` on its own - via a clean
shutdown or an ungraceful crash/network loss alike - without anything else
needing to publish it.

### Home Assistant

Turning on `mqtt.enabled: true` is enough - no Home Assistant-side YAML
required. bark-detector also publishes Home Assistant [MQTT
discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery)
configs (`mqtt.discovery: true` by default), so HA auto-creates:

- Per source: a **Detected** event entity (fires the instant a bark crosses
  threshold, using the `triggered` topic for the lowest latency; `score`,
  the saved snippet's `file` path, and `duration` show up as attributes a
  few seconds later, once the `event` message with that same `id` arrives)
  and a **Connectivity** binary sensor (from the `status` topic).
- One instance-wide **Space Alert** diagnostic entity, from
  `bark_detector/system/space_limit_reached`.

All of these use `bark_detector/bridge/status` as their HA `availability_topic`,
so they show up as `unavailable` in HA (instead of a stale last value) if
bark-detector stops publishing for any reason. Renaming a source in the
settings UI updates its existing HA entities in place - it doesn't create
duplicates. Deleting a source removes its HA entities.

Set `mqtt.discovery: false` (or `MQTT_DISCOVERY=false`) to keep the raw MQTT
topics above but skip Home Assistant auto-discovery entirely - e.g. if
you'd rather hand-write your own HA MQTT sensors.

## Local test rig

`test-rig/` spins up a full, real (not mocked) pipeline: a Mosquitto
broker, a mediamtx RTSP server, a small publisher that loops a labeled
bark/not-bark dataset into it as a live audio stream, a second publisher
that loops the same dataset as raw PCM over a listening TCP socket
(emulating an M5 Atom Echo running `esphome/tcp_audio_server.h`), and
`bark-detector` pulling both - one via `type: rtsp` (`sources/backyard.yaml`)
exactly like a real camera, the other via `type: esphome_tcp`
(`sources/kitchen.yaml`) exactly like real Atom Echo hardware.

```bash
docker compose -f test-rig/docker-compose.yml up --build
```

No manual setup step needed - on first run, `stream-publisher` fetches a
small labeled test set from Google AudioSet (via `stream/download_audioset.py`,
see below) into the bind-mounted `test-rig/dataset/`, and reuses it on
every run after that; the `atom` service reuses that same fetched dataset
rather than fetching its own copy. `bark-detector` waits for both streams to
actually be live before it starts (a Docker healthcheck on each publisher),
so there's no window of failed-connection retries at startup.

- Ground truth is in the filenames: `NN_bark.wav` / `NN_negative.wav`
- Watch detections live: `mosquitto_sub -h localhost -t 'bark_detector/#' -v`
- Watch Home Assistant discovery configs: `mosquitto_sub -h localhost -t 'homeassistant/#' -v`
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
examples/                ready-to-run docker-compose examples (see Quick start)
  basic/                   sources via the settings UI, minimal env config
  extended/                everything env-driven, sources as YAML files, web UI off
requirements.txt
Dockerfile
test-rig/                local end-to-end test rig (mosquitto + mediamtx + publishers)
  dataset/                 fetched test clips - gitignored, not committed
  stream/                  the RTSP test publisher
    download_audioset.py     fetches the labeled test set from AudioSet (see above)
    entrypoint.sh             fetch-if-missing -> build playlist -> publish
  atom/                    the esphome_tcp test publisher (emulates an M5 Atom Echo),
                            reuses stream/'s already-built playlist
```

## Status / not yet done

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
