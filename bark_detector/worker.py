"""Per-source loop: read PCM -> classify -> feed snippet recorder -> publish."""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .audio_source import build_source
from .config import Config, SourceConfig
from .detector import AudioTfl, pcm_chunk_to_waveform
from .event_store import EventStore
from .mqtt_publisher import MqttPublisher
from .snippet import SnippetRecorder

logger = logging.getLogger(__name__)


class SourceWorker(threading.Thread):
    def __init__(
        self,
        config: Config,
        source: SourceConfig,
        publisher: MqttPublisher,
        store: EventStore,
        stop_event: threading.Event,
    ):
        super().__init__(name=f"worker-{source.name}", daemon=True)
        self.config = config
        self.source = source
        self.publisher = publisher
        self.store = store
        self.stop_event = stop_event

        self.audio_source = build_source(source, config.ffmpeg_path, stop_event)
        self.detector = AudioTfl(num_threads=2)
        self.recorder = SnippetRecorder(
            source_name=source.name,
            snippet_dir=config.snippet_dir,
            pre_capture=source.pre_capture,
            post_capture=source.post_capture,
        )

        # last time audio actually arrived (not just "ffmpeg process alive")
        # - read by AppController's status watchdog to decide connected vs
        # disconnected; None means never received anything yet
        self.last_chunk_at: Optional[float] = None

    def run(self) -> None:
        logger.info("[%s] starting worker (type=%s)", self.source.name, self.source.type)
        self.audio_source.start()

        while not self.stop_event.is_set():
            chunk = self.audio_source.read_chunk()
            if chunk is None:
                continue

            self.last_chunk_at = time.time()
            self._process_chunk(chunk)

        self.audio_source.stop()
        logger.info("[%s] worker stopped", self.source.name)

    def _process_chunk(self, chunk: bytes) -> None:
        waveform, rms, _dBFS = pcm_chunk_to_waveform(chunk)

        if rms >= self.source.min_volume:
            for label, score in self.detector.detect(waveform):
                if label not in self.source.listen:
                    continue
                if score < self.source.threshold_for(label):
                    continue
                self.recorder.trigger(label, score)
                break  # one trigger per chunk is enough

        event = self.recorder.add_chunk(chunk)
        if event is not None:
            try:
                self.store.add(event)
            except Exception:
                logger.exception("[%s] failed to store event", self.source.name)

            try:
                self.publisher.publish(event)
            except Exception:
                logger.exception("[%s] failed to publish MQTT event", self.source.name)
