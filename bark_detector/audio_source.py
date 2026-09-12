"""Pluggable sources of raw PCM audio chunks.

Every source yields fixed-size mono 16-bit PCM chunks at AUDIO_SAMPLE_RATE,
so the detection loop in worker.py doesn't need to know where the audio
comes from (an RTSP camera's audio track, a USB/ALSA mic, or an ESPHome
device like an M5 Atom Echo streaming raw PCM over a bespoke TCP protocol -
see esphome/tcp_audio_server.h).
"""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
import time
from typing import Optional

from .audio_format import AUDIO_SAMPLE_RATE, CHUNK_BYTES
from .config import SourceConfig

logger = logging.getLogger(__name__)

STALL_SECONDS = 12.0
READ_POLL_SECONDS = 0.25


def build_ffmpeg_command(
    source_type: str, path: str, input_args: list[str], ffmpeg_path: str
) -> list[str]:
    """Shared between FfmpegAudioSource (the real capture loop) and probe.py
    (the settings page's one-shot 'Test' button), so a probe result actually
    reflects the command that would be used for real."""
    cmd = [ffmpeg_path, "-vn", "-threads", "1"]

    if source_type == "device":
        input_format = "dshow" if _is_windows() else "alsa"
        cmd += ["-f", input_format]
    elif source_type == "rtsp":
        cmd += ["-rtsp_transport", "tcp"]
    elif source_type == "esphome_tcp":
        # raw PCM has no container of its own - tell ffmpeg how to parse the
        # byte stream it reads from the socket. Matches tcp_audio_server.h /
        # atom.yaml's microphone config exactly, so no resampling is needed.
        # `path` is a plain tcp://host:port URL; ffmpeg's tcp protocol
        # connects out as a client by default (no "?listen"), matching the
        # device acting as the TCP server.
        cmd += ["-f", "s16le", "-ar", str(AUDIO_SAMPLE_RATE), "-ac", "1"]

    cmd += list(input_args)
    cmd += ["-i", path]
    cmd += [
        "-threads",
        "1",
        "-f",
        "s16le",
        "-ar",
        str(AUDIO_SAMPLE_RATE),
        "-ac",
        "1",
        "-y",
        "pipe:",
    ]
    return cmd


class AudioSource:
    """Base interface: read_chunk() returns CHUNK_BYTES of s16le mono PCM,
    or None if temporarily unavailable (caller should retry)."""

    def start(self) -> None:
        raise NotImplementedError

    def read_chunk(self) -> Optional[bytes]:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


class FfmpegAudioSource(AudioSource):
    """Extracts audio via ffmpeg, from an RTSP/HTTP URL (a camera's audio
    track), a local capture device (e.g. an ALSA device for a USB mic), or a
    raw-PCM TCP stream (an ESPHome device like an M5 Atom Echo). Restarts
    ffmpeg automatically if it exits or stalls - which also covers an
    esphome_tcp source getting disconnected/replaced by another client, or
    the device rebooting: ffmpeg exits, and this restarts it after
    retry_interval. A stream that stops producing PCM is restarted after
    STALL_SECONDS, even if ffmpeg remains alive."""

    def __init__(
        self,
        source: SourceConfig,
        ffmpeg_path: str,
        stop_event: threading.Event,
        retry_interval: float = 5.0,
    ):
        self.source = source
        self.ffmpeg_path = ffmpeg_path
        self.stop_event = stop_event
        self.retry_interval = retry_interval
        self.process: Optional[subprocess.Popen] = None
        self._chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=2)
        self._reader: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._last_chunk_at = time.monotonic()
        self._failures = 0
        # guards start()/stop() so a controller thread can force-stop this
        # source (e.g. to apply a settings change) while the worker thread's
        # own read/restart loop is running, without racing on self.process
        self._lifecycle_lock = threading.Lock()

    def _build_command(self) -> list[str]:
        return build_ffmpeg_command(
            self.source.type, self.source.path, self.source.input_args, self.ffmpeg_path
        )

    def start(self) -> None:
        if self.stop_event.is_set():
            return
        cmd = self._build_command()
        logger.info("[%s] starting ffmpeg: %s", self.source.name, " ".join(cmd))
        with self._lifecycle_lock:
            if self.stop_event.is_set():
                return
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                bufsize=CHUNK_BYTES * 4,
            )
            self._chunks = queue.Queue(maxsize=2)
            self._reader_stop = threading.Event()
            self._last_chunk_at = time.monotonic()
            self._reader = threading.Thread(
                target=self._read_stdout,
                args=(self.process, self._chunks, self._reader_stop),
                name=f"ffmpeg-reader-{self.source.name}",
                daemon=True,
            )
            self._reader.start()

    @staticmethod
    def _read_stdout(process, chunks: queue.Queue[bytes | None], stopped: threading.Event) -> None:
        try:
            while not stopped.is_set():
                chunk = process.stdout.read(CHUNK_BYTES)
                while not stopped.is_set():
                    try:
                        chunks.put(chunk if len(chunk) == CHUNK_BYTES else None, timeout=READ_POLL_SECONDS)
                        break
                    except queue.Full:
                        pass
                if len(chunk) != CHUNK_BYTES:
                    break
        except Exception:
            logger.exception("error reading ffmpeg stdout")
            try:
                chunks.put_nowait(None)
            except queue.Full:
                pass

    def _restart(self) -> None:
        self.stop()
        if self.stop_event.is_set():
            return
        self._failures += 1
        delay = min(self.retry_interval * (2 ** min(self._failures - 1, 4)), 60.0)
        logger.warning(
            "[%s] ffmpeg unavailable, retrying in %.1fs",
            self.source.name,
            delay,
        )
        if not self.stop_event.wait(delay):
            self.start()

    def read_chunk(self) -> Optional[bytes]:
        # captured once and reused for this whole call: self.process can be
        # concurrently reset to None by a controller thread calling stop(),
        # and a terminated Popen object remains safe to call .poll()/.stdout
        # on, so a stable local reference avoids racing on self.process
        process = self.process
        if process is None:
            self.start()
            process = self.process
            if process is None:
                return None

        try:
            chunk = self._chunks.get(timeout=READ_POLL_SECONDS)
        except queue.Empty:
            if self.stop_event.is_set():
                return None
            if process.poll() is not None or time.monotonic() - self._last_chunk_at >= STALL_SECONDS:
                logger.warning("[%s] ffmpeg exited or audio stalled; restarting", self.source.name)
                self._restart()
            return None

        if chunk is None:
            self._restart()
            return None

        self._last_chunk_at = time.monotonic()
        self._failures = 0
        return chunk

    def stop(self) -> None:
        with self._lifecycle_lock:
            process = self.process
            if process is None:
                return
            self.process = None
            self._reader_stop.set()
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                process.kill()
                process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            reader = self._reader
        if reader is not None:
            reader.join(timeout=1)


def _is_windows() -> bool:
    import platform

    return platform.system() == "Windows"


def build_source(
    source: SourceConfig, ffmpeg_path: str, stop_event: threading.Event
) -> AudioSource:
    if source.type in ("rtsp", "device", "esphome_tcp"):
        return FfmpegAudioSource(source, ffmpeg_path, stop_event)

    raise ValueError(f"unsupported source type: {source.type}")
