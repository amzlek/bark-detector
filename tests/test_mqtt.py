import json
import unittest
from unittest.mock import MagicMock, patch

from bark_detector.config import MqttConfig, build_source_config
from bark_detector.ha_discovery import build_source_discovery
from bark_detector.mqtt_publisher import MqttPublisher
from bark_detector.snippet import DetectionEvent, DetectionTrigger


class MqttTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.publish.return_value.wait_for_publish.return_value = None
        self.client_factory = patch("bark_detector.mqtt_publisher.mqtt.Client", return_value=self.client)
        self.client_factory.start()
        self.addCleanup(self.client_factory.stop)
        self.publisher = MqttPublisher(MqttConfig(enabled=True, topic="dogs"))
        self.source = build_source_config({
            "name": "Kitchen", "type": "rtsp", "path": "rtsp://example",
            "listen": ["bark"], "id": "stable-id",
        })

    def test_trigger_and_event_share_id_and_use_distinct_topics(self):
        trigger = DetectionTrigger("capture-1", "Kitchen", "bark", 0.81234, 123.0)
        event = DetectionEvent("capture-1", "Kitchen", "bark", 0.91234, 123.0,
                               "/media/bark.wav", 2.345)
        self.publisher.publish_trigger(trigger)
        self.publisher.publish_event(event)
        first, second = self.client.publish.call_args_list
        self.assertEqual(first.args[0], "dogs/Kitchen/triggered")
        self.assertEqual(second.args[0], "dogs/Kitchen/event")
        self.assertEqual(json.loads(first.args[1])["id"], json.loads(second.args[1])["id"])
        self.assertEqual(json.loads(second.args[1])["duration"], 2.35)

    def test_status_and_availability_are_retained(self):
        self.client.will_set.assert_called_once_with("dogs/bridge/status", "offline", qos=1, retain=True)
        self.publisher._handle_connect(self.client, None)
        self.client.publish.assert_called_with("dogs/bridge/status", "online", qos=1, retain=True)
        self.client.publish.return_value.wait_for_publish.assert_not_called()
        self.publisher.publish_status("Kitchen", "connected")
        args, kwargs = self.client.publish.call_args
        self.assertEqual(args[0], "dogs/Kitchen/status")
        self.assertEqual(json.loads(args[1])["status"], "connected")
        self.assertTrue(kwargs["retain"])
        self.publisher.stop()
        self.client.publish.assert_called_with("dogs/bridge/status", "offline", qos=1, retain=True)

    def test_discovery_uses_stable_id_and_cleans_up(self):
        initial = build_source_discovery(self.publisher.config, self.source)
        renamed = build_source_config({"name": "New kitchen", "type": "rtsp",
                                       "path": "rtsp://example", "id": self.source.id})
        updated = build_source_discovery(self.publisher.config, renamed)
        self.assertEqual(set(initial), set(updated))
        self.assertNotEqual(next(iter(initial.values()))["state_topic"],
                            next(iter(updated.values()))["state_topic"])
        self.publisher.publish_all_discovery([self.source])
        self.assertEqual(self.client.publish.call_count, 3)
        self.assertTrue(all(call.kwargs["retain"] for call in self.client.publish.call_args_list))
        self.client.publish.reset_mock()
        self.publisher.remove_discovery_for_source(self.source.id)
        self.assertEqual(self.client.publish.call_count, 2)
        self.assertTrue(all(call.args[1] == "" for call in self.client.publish.call_args_list))

    def test_disabled_publisher_does_not_send(self):
        self.publisher.config.enabled = False
        self.publisher.publish_trigger(DetectionTrigger("id", "Kitchen", "bark", 0.9, 0))
        self.publisher.publish_discovery_for_source(self.source)
        self.client.publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
