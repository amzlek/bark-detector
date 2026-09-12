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
        self._last_near_miss_log: dict[str, float] = {}

    def start(self) -> None:
        # Surface ffmpeg spawn/configuration errors to the controller before
        # it commits a source change. Network connection may still be async.
        self.audio_source.start()
        try:
            super().start()
        except BaseException:
            self.audio_source.stop()
            raise

    def run(self) -> None:
        logger.info("[%s] starting worker (type=%s)", self.source.name, self.source.type)
        try:
            while not self.stop_event.is_set():
                chunk = self.audio_source.read_chunk()
                if chunk is None:
                    continue

                self.last_chunk_at = time.time()
                self._process_chunk(chunk)
        finally:
            self.audio_source.stop()

        logger.info("[%s] worker stopped", self.source.name)

    def _process_chunk(self, chunk: bytes) -> None:
        waveform, rms, _dBFS = pcm_chunk_to_waveform(chunk)

        if rms >= self.source.min_volume:
            min_threshold = min(
                max(0.01, self.source.detection_threshold_for(label) - 0.2)
                for label in self.source.listen
            ) if self.source.listen else 1.0
            for label, score in self.detector.detect(waveform, threshold=min_threshold):
                if label not in self.source.listen:
                    continue
                if self.recorder.active_label is not None and label != self.recorder.active_label:
                    continue
                detection_threshold = self.source.detection_threshold_for(label)
                if score < detection_threshold:
                    if score >= max(0.01, detection_threshold - 0.2):
                        now = time.monotonic()
                        if now - self._last_near_miss_log.get(label, float('-inf')) >= 60:
                            logger.info(
                                "[%s] below detection threshold: label=%s score=%.3f detection=%.3f notify=%.3f rms=%.1f",
                                self.source.name, label, score, detection_threshold,
                                self.source.notify_threshold_for(label), rms,
                            )
                            self._last_near_miss_log[label] = now
                    continue
                self.recorder.trigger(label, score)
                if score >= self.source.notify_threshold_for(label):
                    trigger = self.recorder.notify(label, score)
                else:
                    trigger = None
                if trigger is not None:
                    try:
                        self.publisher.publish_trigger(trigger)
                    except Exception:
                        logger.exception(
                            "[%s] failed to publish MQTT trigger", self.source.name
                        )
                break  # one trigger per chunk is enough

        event = self.recorder.add_chunk(chunk)
        if event is not None:
            try:
                self.store.add(event)
            except Exception:
                logger.exception("[%s] failed to store event", self.source.name)

            if event.notified:
                try:
                    self.publisher.publish_event(event)
                except Exception:
                    logger.exception("[%s] failed to publish MQTT event", self.source.name)
            else:
                logger.info(
                    "[%s] saved without MQTT notification: label=%s peak=%.3f detection=%.3f notify=%.3f rms=%.1f",
                    self.source.name, event.label, event.score,
                    self.source.detection_threshold_for(event.label),
                    self.source.notify_threshold_for(event.label), rms,
                )
