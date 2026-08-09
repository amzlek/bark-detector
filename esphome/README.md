# Atom Echo — continuous mic streaming firmware

ESPHome firmware for an M5Stack Atom Echo that continuously captures audio and
serves it over a raw TCP socket for an external consumer (e.g. bark-detector),
instead of acting as a Home Assistant voice assistant. It also exposes a
"Recording" on/off switch and a manual test-beep button.

## Files

- `atom.yaml` — the ESPHome config.
- `tcp_audio_server.h` — single-client raw-PCM TCP server (referenced via
  `esphome: includes:`).
- `beep_tone.h` — minimal sine tone generator used by the Beep button.

**The `.h` files must live in the same directory as `atom.yaml`** on whatever
machine/dashboard actually compiles it. ESPHome resolves `includes:` entries
relative to the yaml file, not the repo root — if you copy `atom.yaml`
somewhere without the two headers next to it, the build will fail to find
them. Keep all three files together as a unit.

## Before flashing

Edit the substitutions at the top of `atom.yaml`:

- `name` / `friendly_name` — device identity.
- `stream_port` — TCP port the audio server listens on (default `12345`).
- `beep_frequency_hz` / `beep_duration_ms` — test beep tone (duration is
  hard-capped at 300ms in code regardless of what's set here — see "Beep
  button" below for why).
- `manual_ip` — set a real static IP for your network, or replace the block
  with `wifi: ap:`/DHCP per the ESPHome docs if you don't want a static IP.

You'll also need a `secrets.yaml` (in the ESPHome dashboard's config dir) with
`wifi_ssid`, `wifi_password`, `encrypt_key`, and `ota_pass`.

## Audio streaming protocol

The device is a **TCP server**, not a client — the consumer connects out to
`<device-ip>:<stream_port>`.

- Raw PCM, no container or framing: signed 16-bit little-endian, mono, 16kHz.
- Bytes flow continuously for as long as a client is connected and the
  `Recording` switch is on.
- **Single client only.** A new incoming connection immediately closes
  whatever client was previously attached (see `poll_accept()` in
  `tcp_audio_server.h`). If your consumer's connection drops unexpectedly,
  it was probably preempted by another client (e.g. manual testing) —
  reconnect, don't treat it as fatal.
- When `Recording` is off, the TCP connection stays open but no bytes are
  sent — that's expected, not an error. The stream just resumes when
  recording turns back on.
- Test manually with: `ffplay -f s16le -ar 16000 -ac 1 -i tcp://<ip>:<port>`

## Recording switch and button

- `Recording` (HA-exposed switch) starts/stops `microphone.capture` and
  drives the LED:
  - **Green** — recording (capturing + streaming).
  - **Blue** — idle (mic stopped, WiFi fine).
  - **Red, pulsing** — WiFi disconnected.
- The physical button (GPIO39) is wired to a **short press only** (under 1s):
  it toggles the same `Recording` switch. There's no long-press action.

## Beep button and the mic/speaker conflict

The mic and speaker share the same I2S bus/clock pins on this board (only one
physical I2S peripheral is wired to both), so they can't run at the same
time. Pressing `Beep`:

1. Stops mic capture and waits 300ms for the bus to actually release.
2. Calls `speaker->start()`, then waits a real 100ms (a YAML `delay:`, not a
   busy loop) before writing any audio.
3. Plays the tone via `beep_tone::play_tone`.
4. Waits for playback to finish, then resumes mic capture — but only if
   `Recording` was on beforehand.

Step 2's delay isn't cosmetic: calling `play()` immediately after `start()`
deadlocks the whole device. The speaker only transitions from "starting" to
"running" when the main loop gets to run again, and a busy-wait for that
transition inside the same automation blocks the very loop iteration that
would make it happen. A real YAML `delay:` (which yields back to the
scheduler) is required between the two steps — a C++ `delay()` call inside a
lambda does not have the same effect.

Pressing Beep will log benign `took a long time for an operation` warnings
for the `button`/`wifi` components — that's just ESPHome noticing that
writing the tone is a synchronous ~300-500ms operation, not a real problem.

### Why the tone is capped and quiet by default

An earlier version of this firmware drove the speaker with a hard-clipped,
gain-boosted, multi-second tone (and later a full-scale square wave) while
chasing a "why is this so quiet" problem — and it damaged the amp/speaker on
one unit. `beep_tone.h` now:

- Generates a clean, unclipped sine (never multiplies past the waveform's
  natural range).
- Defaults to ~61% amplitude, not full scale.
- Hard-caps duration to 300ms **inside the function itself**, regardless of
  what's passed in — so a bad substitution value can't turn into a long
  continuous drive.

Don't raise the gain/amplitude or remove the duration cap without a good
reason — a tiny consumer-grade speaker/amp like this one is built for short,
low-duty-cycle speech bursts, not sustained near-full-power tones.
