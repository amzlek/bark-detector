"""One-shot connectivity probe for a candidate audio source: spawns the same
ffmpeg command a real SourceWorker would use, waits briefly for audio to
actually arrive, then tears it down. Used by the settings page's 'Test'
button so a broken path/device is caught before a source is saved."""

from __future__ import annotations

import logging
import queue
import subprocess
import threading

from .audio_format import CHUNK_BYTES
from .audio_source import build_ffmpeg_command

logger = logging.getLogger(__name__)


def probe_source(
    source_type: str,
    path: str,
    input_args: list[str],
    ffmpeg_path: str,
    timeout: float = 5.0,
) -> tuple[bool, str]:
    cmd = build_ffmpeg_command(source_type, path, input_args, ffmpeg_path)
    logger.info("probing source: %s", " ".join(cmd))

    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL
        )
    except OSError as exc:
        return False, f"failed to start ffmpeg: {exc}"

    # read on a helper thread so a hung/silent source can't block this call
    # past `timeout` - terminate() below unblocks the read either way
    chunk_queue: queue.Queue = queue.Queue(maxsize=1)

    def _read() -> None:
        try:
            # stdout=PIPE was passed above, so this is never actually None -
            # the assert just narrows the type for the checker
            assert process.stdout is not None
            chunk_queue.put(process.stdout.read(CHUNK_BYTES))
        except Exception:
            chunk_queue.put(b"")

    threading.Thread(target=_read, daemon=True).start()

    try:
        chunk = chunk_queue.get(timeout=timeout)
    except queue.Empty:
        chunk = None
    finally:
        try:
            process.terminate()
            process.wait(timeout=3)
        except Exception:
            process.kill()

    if chunk:
        return True, f"received {len(chunk)} bytes of audio"

    stderr = b""
    try:
        if process.stderr is not None:
            stderr = process.stderr.read(4000) or b""
    except Exception:
        pass

    message = _strip_ffmpeg_banner(stderr.decode(errors="replace"))[-500:]
    return False, message or "no audio received before timeout"


def _strip_ffmpeg_banner(stderr: str) -> str:
    """Drop ffmpeg's version/library banner lines so the message returned
    to the settings page's 'Test' button is just the actual error. ffmpeg
    indents most of these with leading spaces, so prefixes are matched
    against the stripped line, not the raw one - the giant 'configuration:'
    line in particular needs this or it survives and eats the [-500:]
    truncation budget below, mangling the actually-useful error lines."""
    banner_prefixes = ("ffmpeg version", "built with", "configuration:", "lib")
    lines = [
        line
        for line in stderr.splitlines()
        if line.strip() and not line.strip().startswith(banner_prefixes)
    ]
    return "\n".join(lines).strip()
