import unittest
from unittest.mock import MagicMock, patch

from bark_detector.config import Config, ConfigError, MqttConfig, load_sources
from bark_detector.controller import AppController
from test_support import ScratchTestCase


class ControllerTests(ScratchTestCase):
    def setUp(self):
        super().setUp()
        self.source_dir = self.scratch / "sources"
        config = Config(mqtt=MqttConfig(password="keep-me"), sources=[],
                        sources_dir=str(self.source_dir), snippet_dir=str(self.scratch))
        self.publisher = MagicMock()
        self.publisher.is_connected.return_value = False
        publisher_patch = patch("bark_detector.controller.MqttPublisher", return_value=self.publisher)
        worker_patch = patch("bark_detector.controller.SourceWorker")
        publisher_patch.start()
        self.worker_class = worker_patch.start()
        self.worker_class.return_value.is_alive.return_value = False
        self.addCleanup(worker_patch.stop)
        self.addCleanup(publisher_patch.stop)
        self.controller = AppController(config, str(self.scratch / "config.yaml"), str(self.source_dir))
        self.addCleanup(self.controller.store.close)
        self.raw = {"name": "Kitchen", "type": "rtsp", "path": "rtsp://example"}

    def test_add_rename_delete_preserves_identity_and_disk_state(self):
        source = self.controller.add_source(self.raw)
        self.assertEqual(load_sources(str(self.source_dir))[0].id, source.id)
        self.publisher.publish_discovery_for_source.assert_called_with(source)
        with self.assertRaises(ConfigError):
            self.controller.add_source(self.raw)

        renamed = self.controller.update_source("Kitchen", {**self.raw, "name": "Porch"})
        self.assertEqual(renamed.id, source.id)
        self.assertEqual([item.name for item in load_sources(str(self.source_dir))], ["Porch"])
        self.assertEqual(len(list(self.source_dir.glob("*.yaml"))), 1)
        self.controller.delete_source("Porch")
        self.assertEqual(load_sources(str(self.source_dir)), [])
        self.publisher.remove_discovery_for_source.assert_called_with(source.id)

    def test_mqtt_partial_update_preserves_password_and_discovery_cleanup(self):
        source = self.controller.add_source(self.raw)
        updated = self.controller.update_mqtt({"host": "new-broker", "discovery": False})
        self.assertEqual(updated.password, "keep-me")
        self.assertEqual(updated.host, "new-broker")
        self.assertFalse(updated.discovery)
        self.publisher.reconfigure.assert_called_with(updated)
        self.publisher.remove_system_discovery.assert_called_once()
        self.publisher.remove_discovery_for_source.assert_called_with(source.id)
        self.assertTrue((self.scratch / "config.yaml").exists())

    def test_source_probe_does_not_save(self):
        with patch("bark_detector.controller.probe_source", return_value=(True, "audio received")) as probe:
            self.assertEqual(self.controller.test_source(self.raw), (True, "audio received"))
        probe.assert_called_once()
        self.assertEqual(self.controller.list_sources(), [])
        self.assertFalse(self.source_dir.exists())
        ok, message = self.controller.test_source({"type": "invalid", "path": "x"})
        self.assertFalse(ok)
        self.assertIn("invalid type", message)


if __name__ == "__main__":
    unittest.main()
