import json
import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

from bark_detector.config import Config, ConfigError, MqttConfig, build_source_config
from bark_detector.controller import AppController
from bark_detector.web import _dispatch, create_app
from test_support import ScratchTestCase


class SocketCollector:
    def __init__(self):
        self.messages = []

    def send(self, raw):
        self.messages.append(json.loads(raw))


class WebTests(ScratchTestCase):
    def setUp(self):
        super().setUp()
        self.source = build_source_config({"name": "Kitchen", "type": "rtsp", "path": "rtsp://example"})
        self.config = Config(mqtt=MqttConfig(password="secret"), sources=[self.source],
                             snippet_dir=str(self.scratch))
        self.controller = SimpleNamespace(
            config=self.config,
            list_sources=Mock(return_value=[self.source]),
            all_source_status=Mock(return_value={"Kitchen": "connected"}),
            get_stats=Mock(return_value={"recordings": 1}),
            get_mqtt=Mock(return_value=self.config.mqtt),
            test_source=Mock(return_value=(True, "connected")),
            add_source=Mock(return_value=self.source),
        )
        self.app_controller = cast(AppController, self.controller)
        self.client = create_app(self.app_controller).test_client()

    def test_pages_assets_and_read_only_api(self):
        for path in ("/", "/settings", "/style.css", "/ws-client.js"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                response.close()
        sources = self.client.get("/api/sources").get_json()
        self.assertEqual(sources[0]["status"], "connected")
        self.assertEqual(self.client.get("/api/stats").get_json()["recordings"], 1)
        self.assertEqual(self.client.post("/api/sources").status_code, 405)

    def test_snippet_download_and_missing_file(self):
        (self.scratch / "sample.wav").write_bytes(b"RIFF-data")
        response = self.client.get("/snippets/sample.wav")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"RIFF-data")
        response.close()
        self.assertEqual(self.client.get("/snippets/missing.wav").status_code, 404)

    def test_websocket_dispatch_redacts_password_and_correlates_request(self):
        socket = SocketCollector()
        _dispatch(self.app_controller, socket, json.dumps({"type": "mqtt.get", "id": "req-1"}))
        reply = socket.messages[0]
        self.assertEqual(reply["id"], "req-1")
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["data"]["has_password"])
        self.assertNotIn("password", reply["data"])
        _dispatch(self.app_controller, socket, json.dumps({"type": "sources.test", "id": "req-2", "payload": {}}))
        self.assertTrue(socket.messages[1]["data"]["ok"])

    def test_websocket_dispatch_rejects_invalid_input(self):
        socket = SocketCollector()
        _dispatch(self.app_controller, socket, "not json")
        self.assertEqual(socket.messages[-1]["type"], "error")
        _dispatch(self.app_controller, socket, json.dumps({"type": "unknown", "id": "req"}))
        self.assertFalse(socket.messages[-1]["ok"])
        self.controller.add_source.side_effect = ConfigError("invalid source")
        _dispatch(self.app_controller, socket, json.dumps({"type": "sources.add", "id": "add", "payload": {}}))
        self.assertEqual(socket.messages[-1]["error"], "invalid source")


if __name__ == "__main__":
    unittest.main()
