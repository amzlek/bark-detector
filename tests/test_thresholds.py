import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

import numpy as np

from bark_detector.audio_format import CHUNK_BYTES
from bark_detector.config import ConfigError, build_source_config
from bark_detector.detector import AudioTfl
from bark_detector.snippet import DetectionEvent, DetectionTrigger, SnippetRecorder
from bark_detector.worker import SourceWorker


class FakeDetector(AudioTfl):
    def __init__(self) -> None:
        self.scores: list[float] = []

    def detect(self, waveform: np.ndarray, threshold: float = 0.5) -> list[tuple[str, float]]:
        return [("bark", score) for score in self.scores if score >= threshold]


class FakeInterpreter:
    def set_tensor(self, _index: int, _waveform: np.ndarray) -> None:
        pass

    def invoke(self) -> None:
        pass

    def get_tensor(self, _index: int) -> np.ndarray:
        scores = np.zeros((1, 30), dtype=np.float32)
        scores[0, 0] = 0.4
        return scores


class EventCollector:
    def __init__(self) -> None:
        self.items: list[DetectionEvent] = []

    def add(self, item: DetectionEvent) -> None:
        self.items.append(item)


class PublisherCollector:
    def __init__(self) -> None:
        self.items: list[DetectionTrigger | DetectionEvent] = []

    def publish_trigger(self, item: DetectionTrigger) -> None:
        self.items.append(item)

    def publish_event(self, item: DetectionEvent) -> None:
        self.items.append(item)


class ThresholdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.tmp.cleanup)
        self.source = build_source_config({
            "name": "test", "type": "rtsp", "path": "rtsp://example/test",
            "listen": ["bark"], "min_volume": 0, "pre_capture": 0,
            "post_capture": 1.95, "detection_thresholds": {"bark": 0.6},
            "notify_thresholds": {"bark": 0.8},
        })
        # Bypass the real worker constructor, which loads the TFLite model.
        # The worker's declared concrete dependencies are replaced by fakes.
        self.worker = cast(Any, SourceWorker.__new__(SourceWorker))
        self.worker.source = self.source
        self.detector = FakeDetector()
        self.worker.detector = self.detector
        self.worker.recorder = SnippetRecorder("test", self.tmp.name, 0, 1.95)
        self.store = EventCollector()
        self.publisher = PublisherCollector()
        self.worker.store = self.store
        self.worker.publisher = self.publisher
        self.worker._last_near_miss_log = {}
        self.chunk = bytes(CHUNK_BYTES)

    def feed(self, score: float | None) -> None:
        self.detector.scores = [score] if score is not None else []
        self.worker._process_chunk(self.chunk)

    def test_below_detection_saves_nothing(self):
        self.feed(0.59)
        self.assertEqual(self.store.items, [])
        self.assertEqual(self.publisher.items, [])

    def test_between_thresholds_saves_without_mqtt(self):
        self.feed(0.65)
        self.feed(None)
        self.assertEqual(len(self.store.items), 1)
        self.assertTrue(Path(self.store.items[0].file).is_file())
        self.assertEqual(self.publisher.items, [])

    def test_notify_at_start(self):
        self.feed(0.85)
        self.feed(None)
        self.assertEqual(len(self.store.items), 1)
        trigger, event = self.publisher.items
        self.assertIsInstance(trigger, DetectionTrigger)
        self.assertIsInstance(event, DetectionEvent)
        self.assertEqual(trigger.id, event.id)
        self.assertEqual(event.id, self.store.items[0].id)

    def test_notify_after_capture_starts(self):
        self.feed(0.65)
        self.feed(0.85)
        self.assertEqual(len(self.publisher.items), 1)
        self.feed(None)
        trigger, event = self.publisher.items
        self.assertIsInstance(trigger, DetectionTrigger)
        self.assertIsInstance(event, DetectionEvent)
        self.assertEqual(trigger.id, event.id)
        self.assertEqual(len(self.store.items), 1)

    def test_legacy_threshold_remains_both_thresholds(self):
        source = build_source_config({
            "name": "old", "type": "rtsp", "path": "rtsp://example/test",
            "listen": ["bark"], "thresholds": {"bark": 0.75},
        })
        self.assertEqual(source.detection_threshold_for("bark"), 0.75)
        self.assertEqual(source.notify_threshold_for("bark"), 0.75)

    def test_notify_below_detection_is_rejected(self):
        with self.assertRaises(ConfigError):
            build_source_config({
                "name": "bad", "type": "rtsp", "path": "rtsp://example/test",
                "listen": ["bark"], "detection_thresholds": {"bark": 0.8},
                "notify_thresholds": {"bark": 0.6},
            })

    def test_classifier_keeps_scores_below_old_fixed_floor(self):
        detector = cast(Any, AudioTfl.__new__(AudioTfl))
        detector.interpreter = FakeInterpreter()
        detector.tensor_input_details = [{"index": 0}]
        detector.tensor_output_details = [{"index": 0}]
        detector.labels = {0: "bark"}
        self.assertEqual(len(detector.detect(np.zeros((1, 15600), dtype=np.float32), 0.3)), 1)


if __name__ == "__main__":
    unittest.main()
