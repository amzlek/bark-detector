"""Ring-buffer + WAV writer: turns a detection into a saved audio snippet
covering `pre_capture` seconds before it and `post_capture` seconds after."""

from __future__ import annotations

import logging
import math
import os
import time
import uuid
import wave
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from .audio_format import AUDIO_DURATION, AUDIO_SAMPLE_RATE

logger = logging.getLogger(__name__)


@dataclass
class DetectionTrigger:
    """Published immediately when a capture starts - before any audio has
    finished recording. `id` correlates it with the DetectionEvent that
    follows once the snippet is saved."""

    id: str
    source: str
    label: str
    score: float
    timestamp: float


@dataclass
class DetectionEvent:
    id: str
    source: str
    label: str
    score: float
    timestamp: float
    file: str
    duration: float


@dataclass
class _ActiveCapture:
    id: str
    label: str
    score: float
    timestamp: float
    pre_chunks: list[bytes]
    post_chunks: list[bytes] = field(default_factory=list)
    chunks_remaining: int = 0


class SnippetRecorder:
    def __init__(
        self,
        source_name: str,
        snippet_dir: str,
        pre_capture: float,
        post_capture: float,
    ):
        self.source_name = source_name
        self.snippet_dir = snippet_dir
        self.pre_chunks_max = max(1, math.ceil(pre_capture / AUDIO_DURATION))
        self.post_chunks_needed = max(1, math.ceil(post_capture / AUDIO_DURATION))
        self.pre_buffer: deque[bytes] = deque(maxlen=self.pre_chunks_max)
        self.active: Optional[_ActiveCapture] = None

    @property
    def is_capturing(self) -> bool:
        return self.active is not None

    def trigger(self, label: str, score: float) -> Optional[DetectionTrigger]:
        """Start (or extend, if already capturing) a snippet capture.
        Returns a DetectionTrigger only when a NEW capture starts - the id/
        label/timestamp minted here are carried through unchanged to the
        DetectionEvent add_chunk() eventually returns, so a subscriber can
        correlate the two. Extending an already-active capture (the bark
        continues) returns None: one 'triggered' message per capture, not
        one per chunk."""
        now = time.time()
        if self.active is None:
            trigger_id = uuid.uuid4().hex
            self.active = _ActiveCapture(
                id=trigger_id,
                label=label,
                score=score,
                timestamp=now,
                pre_chunks=list(self.pre_buffer),
                chunks_remaining=self.post_chunks_needed,
            )
            logger.info(
                "[%s] triggered by '%s' (score=%.2f)", self.source_name, label, score
            )
            return DetectionTrigger(
                id=trigger_id,
                source=self.source_name,
                label=label,
                score=score,
                timestamp=now,
            )
        else:
            self.active.chunks_remaining = self.post_chunks_needed
            self.active.score = max(self.active.score, score)
            return None

    def add_chunk(self, chunk: bytes) -> Optional[DetectionEvent]:
        """Feed the next PCM chunk in. Always keeps the pre-roll buffer warm;
        if a capture is active, appends to it and finalizes/writes the WAV
        once enough post-roll audio has been collected."""
        self.pre_buffer.append(chunk)

        if self.active is None:
            return None

        self.active.post_chunks.append(chunk)
        self.active.chunks_remaining -= 1

        if self.active.chunks_remaining > 0:
            return None

        event = self._finalize()
        self.active = None
        return event

    def _finalize(self) -> DetectionEvent:
        capture = self.active
        assert capture is not None
        all_chunks = capture.pre_chunks + capture.post_chunks
        duration = len(all_chunks) * AUDIO_DURATION

        os.makedirs(self.snippet_dir, exist_ok=True)
        filename = (
            f"{self.source_name}_{capture.label}_{int(capture.timestamp)}.wav"
        )
        filepath = os.path.join(self.snippet_dir, filename)

        with wave.open(filepath, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(AUDIO_SAMPLE_RATE)
            wav_file.writeframes(b"".join(all_chunks))

        logger.info(
            "[%s] saved snippet %s (%.1fs)", self.source_name, filepath, duration
        )

        return DetectionEvent(
            id=capture.id,
            source=self.source_name,
            label=capture.label,
            score=capture.score,
            timestamp=capture.timestamp,
            file=filepath,
            duration=duration,
        )
