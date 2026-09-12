import time
import unittest
import wave

from bark_detector.audio_format import AUDIO_DURATION, AUDIO_SAMPLE_RATE, CHUNK_BYTES
from bark_detector.cleanup import compute_stats, run_cleanup
from bark_detector.config import CleanupConfig
from bark_detector.event_store import EventStore
from bark_detector.mqtt_publisher import MqttPublisher
from bark_detector.snippet import DetectionEvent, SnippetRecorder
from test_support import ScratchTestCase


class AlertCollector(MqttPublisher):
    def __init__(self):
        self.alerts: list[tuple[int, int, int]] = []

    def publish_space_alert(self, used_bytes: int, max_bytes: int, deleted_count: int) -> None:
        self.alerts.append((used_bytes, max_bytes, deleted_count))


class StorageTests(ScratchTestCase):
    def setUp(self):
        super().setUp()
        self.store = EventStore(str(self.scratch / "events.db"))
        self.addCleanup(self.store.close)

    def test_capture_writes_valid_wav_and_keeps_peak_score(self):
        recorder = SnippetRecorder("Kitchen", str(self.scratch), AUDIO_DURATION, AUDIO_DURATION)
        chunk = bytes(CHUNK_BYTES)
        recorder.add_chunk(chunk)
        trigger = recorder.trigger("bark", 0.6)
        assert trigger is not None
        recorder.trigger("bark", 0.9)
        self.assertIsNone(recorder.notify("howl", 0.9))
        notification = recorder.notify("bark", 0.9)
        assert notification is not None
        self.assertEqual(notification.id, trigger.id)
        self.assertIsNone(recorder.notify("bark", 0.9))
        event = recorder.add_chunk(chunk)
        assert event is not None
        self.assertEqual(event.score, 0.9)
        self.assertTrue(event.notified)
        self.assertEqual(event.id, trigger.id)
        with wave.open(event.file, "rb") as wav_file:
            self.assertEqual(wav_file.getframerate(), AUDIO_SAMPLE_RATE)
            self.assertEqual(wav_file.getnchannels(), 1)
            self.assertEqual(wav_file.getnframes(), 2 * CHUNK_BYTES // 2)

    def test_store_orders_pages_and_deletes(self):
        for index in range(3):
            self.store.add(DetectionEvent(str(index), "Kitchen", "bark", 0.8, float(index),
                                          str(self.scratch / f"{index}.wav"), 1.0))
        self.assertEqual(self.store.count(), 3)
        self.assertEqual([row["timestamp"] for row in self.store.list_recent(limit=2)], [2.0, 1.0])
        self.assertEqual([row["timestamp"] for row in self.store.list_recent(before=2)], [1.0, 0.0])
        oldest = self.store.list_all_ordered_by_age_asc()[0]
        self.store.delete_ids([oldest["id"]])
        self.assertEqual(self.store.count(), 2)

    def test_age_cleanup_removes_old_file_and_row(self):
        old_file = self.scratch / "old.wav"
        new_file = self.scratch / "new.wav"
        old_file.write_bytes(b"old")
        new_file.write_bytes(b"new")
        now = time.time()
        for name, stamp in ((old_file, now - 3 * 86400), (new_file, now)):
            self.store.add(DetectionEvent(name.stem, "Kitchen", "bark", 0.8, stamp, str(name), 1.0))
        alerts = AlertCollector()
        run_cleanup(self.store, str(self.scratch), CleanupConfig(max_age_days=1, max_space_mb=None), alerts)
        self.assertFalse(old_file.exists())
        self.assertTrue(new_file.exists())
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(alerts.alerts, [])

    def test_space_cleanup_evicts_oldest_and_alerts(self):
        for index in range(3):
            path = self.scratch / f"{index}.wav"
            path.write_bytes(b"x" * 1000)
            self.store.add(DetectionEvent(str(index), "Kitchen", "bark", 0.8,
                                          float(index), str(path), 1.0))
        alerts = AlertCollector()
        run_cleanup(self.store, str(self.scratch), CleanupConfig(max_age_days=None, max_space_mb=0.002), alerts)
        self.assertFalse((self.scratch / "0.wav").exists())
        self.assertEqual(self.store.count(), 2)
        self.assertEqual(alerts.alerts, [(3000, 2000, 1)])
        stats = compute_stats(self.store, str(self.scratch))
        self.assertEqual(stats["recordings"], 2)
        self.assertEqual(stats["space_used_bytes"], 2000)


if __name__ == "__main__":
    unittest.main()
