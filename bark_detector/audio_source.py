"""Pluggable sources of raw PCM audio chunks.

Every source yields fixed-size mono 16-bit PCM chunks at AUDIO_SAMPLE_RATE,
so the detection loop in worker.py doesn't need to know where the audio
comes from (an RTSP camera's audio track, a USB/ALSA mic, or eventually a
Wyoming-protocol satellite like an M5 Atom Echo).
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Optional

from .audio_format import AUDIO_SAMPLE_RATE, CHUNK_BYTES
from .config import SourceConfig

logger = logging.getLogger(__name__)


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
    """Extracts audio via ffmpeg, from either an RTSP/HTTP URL (a camera's
    audio track) or a local capture device (e.g. an ALSA device for a USB
    mic). Restarts ffmpeg automatically if it exits or stalls."""

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
        # guards start()/stop() so a controller thread can force-stop this
        # source (e.g. to apply a settings change) while the worker thread's
        # own read/restart loop is running, without racing on self.process
        self._lifecycle_lock = threading.Lock()

    def _build_command(self) -> list[str]:
        return build_ffmpeg_command(
            self.source.type, self.source.path, self.source.input_args, self.ffmpeg_path
        )

    def start(self) -> None:
        cmd = self._build_command()
        logger.info("[%s] starting ffmpeg: %s", self.source.name, " ".join(cmd))
        with self._lifecycle_lock:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                bufsize=CHUNK_BYTES * 4,
            )

    def _restart(self) -> None:
        self.stop()
        if self.stop_event.is_set():
            return
        logger.warning(
            "[%s] ffmpeg unavailable, retrying in %.1fs",
            self.source.name,
            self.retry_interval,
        )
        time.sleep(self.retry_interval)
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
            chunk = process.stdout.read(CHUNK_BYTES)
        except Exception as exc:
            logger.error("[%s] error reading from ffmpeg: %s", self.source.name, exc)
            self._restart()
            return None

        if not chunk or len(chunk) < CHUNK_BYTES:
            if process.poll() is not None:
                logger.error(
                    "[%s] ffmpeg exited (code %s), restarting",
                    self.source.name,
                    process.returncode,
                )
                self._restart()
            return None

        return chunk

    def stop(self) -> None:
        with self._lifecycle_lock:
            process = self.process
            if process is None:
                return
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                process.kill()
            finally:
                self.process = None


def _is_windows() -> bool:
    import platform

    return platform.system() == "Windows"


def build_source(
    source: SourceConfig, ffmpeg_path: str, stop_event: threading.Event
) -> AudioSource:
    if source.type in ("rtsp", "device"):
        return FfmpegAudioSource(source, ffmpeg_path, stop_event)

    raise ValueError(f"unsupported source type: {source.type}")
