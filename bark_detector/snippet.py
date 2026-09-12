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
    notified: bool = False


@dataclass
class _ActiveCapture:
    id: str
    label: str
    score: float
    timestamp: float
    pre_chunks: list[bytes]
    post_chunks: list[bytes] = field(default_factory=list)
    chunks_remaining: int = 0
    notified: bool = False


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

    @property
    def active_label(self) -> str | None:
        return self.active.label if self.active is not None else None

    def trigger(self, label: str, score: float) -> Optional[DetectionTrigger]:
        """Start or extend a capture at the detection threshold.
        The worker calls notify() separately if the notify threshold is met."""
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

    def notify(self, label: str, score: float) -> Optional[DetectionTrigger]:
        """Mark an active capture as notified once, when its label qualifies."""
        capture = self.active
        if capture is None or capture.notified or capture.label != label:
            return None
        capture.notified = True
        return DetectionTrigger(
            id=capture.id,
            source=self.source_name,
            label=capture.label,
            score=score,
            timestamp=time.time(),
        )

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
            notified=capture.notified,
        )
