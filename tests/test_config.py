import os
import unittest
from unittest.mock import patch

import yaml

from bark_detector.config import (
    ConfigError,
    build_source_config,
    delete_source_file,
    dump_config,
    load_config,
    load_sources,
    save_config,
    save_source,
)
from test_support import ScratchTestCase


class ConfigTests(ScratchTestCase):
    def test_missing_config_uses_defaults_without_writing(self):
        config_path = self.scratch / "missing.yaml"
        sources_dir = self.scratch / "sources"
        config = load_config(str(config_path), str(sources_dir))
        self.assertFalse(config.mqtt.enabled)
        self.assertEqual(config.sources, [])
        self.assertFalse(config_path.exists())
        self.assertFalse(sources_dir.exists())

    def test_source_round_trip_and_stable_id(self):
        sources_dir = self.scratch / "sources"
        source = build_source_config({
            "name": "Kitchen", "type": "rtsp", "path": "rtsp://example/stream",
            "listen": ["bark"], "detection_thresholds": {"bark": 0.5},
            "notify_thresholds": {"bark": 0.9},
        })
        save_source(source, str(sources_dir))
        loaded = load_sources(str(sources_dir))
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].id, source.id)
        self.assertEqual(loaded[0].detection_threshold_for("bark"), 0.5)
        self.assertEqual(loaded[0].notify_threshold_for("bark"), 0.9)
        delete_source_file("Kitchen", str(sources_dir))
        self.assertEqual(load_sources(str(sources_dir)), [])

    def test_legacy_source_is_migrated_once(self):
        sources_dir = self.scratch / "sources"
        sources_dir.mkdir()
        source_file = sources_dir / "kitchen.yaml"
        source_file.write_text("name: Kitchen\ntype: rtsp\npath: rtsp://example/stream\nthresholds:\n  bark: 0.7\n")
        first = load_sources(str(sources_dir))[0]
        second = load_sources(str(sources_dir))[0]
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.detection_threshold_for("bark"), 0.7)
        self.assertEqual(first.notify_threshold_for("bark"), 0.7)
        self.assertEqual(yaml.safe_load(source_file.read_text())["id"], first.id)

    def test_env_override_is_not_persisted(self):
        config_file = self.scratch / "config.yaml"
        config_file.write_text("mqtt:\n  enabled: true\n  password: file-secret\n")
        with patch.dict(os.environ, {"MQTT_PASSWORD": "env-secret", "MQTT_HOST": "broker"}):
            config = load_config(str(config_file), str(self.scratch / "sources"))
            self.assertEqual(config.mqtt.password, "env-secret")
            self.assertEqual(config.mqtt.host, "broker")
            saved = dump_config(config)
            self.assertNotIn("password", saved["mqtt"])
            self.assertNotIn("host", saved["mqtt"])
            save_config(config, str(config_file))
            self.assertNotIn("env-secret", config_file.read_text())

    def test_rejects_invalid_sources_and_thresholds(self):
        base = {"name": "Kitchen", "type": "rtsp", "path": "rtsp://example", "listen": ["bark"]}
        for changes in (
            {"name": " Kitchen"}, {"name": "A/B"}, {"type": "invalid"},
            {"notify_thresholds": {"bark": 1.1}},
            {"detection_thresholds": {"bark": 0.9}, "notify_thresholds": {"bark": 0.8}},
            {"notify_thresholds": {"bark": "not-a-number"}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ConfigError):
                build_source_config({**base, **changes})

    def test_duplicate_source_names_fail(self):
        sources_dir = self.scratch / "sources"
        sources_dir.mkdir()
        body = "name: Kitchen\ntype: rtsp\npath: rtsp://example\nid: stable\n"
        (sources_dir / "a.yaml").write_text(body)
        (sources_dir / "b.yaml").write_text(body)
        with self.assertRaises(ConfigError):
            load_sources(str(sources_dir))


if __name__ == "__main__":
    unittest.main()
