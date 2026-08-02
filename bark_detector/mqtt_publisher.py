"""Publishes detection events to MQTT as JSON metadata + a snippet file path."""

from __future__ import annotations

import json
import logging
import threading
import time

import paho.mqtt.client as mqtt

from .config import MqttConfig
from .snippet import DetectionEvent

logger = logging.getLogger(__name__)


def _build_client(config: MqttConfig) -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=config.client_id)
    if config.username:
        client.username_pw_set(config.username, config.password)
    return client


class MqttPublisher:
    """Thread-safe: publish() is called concurrently from every SourceWorker,
    and reconfigure() may swap the broker connection at runtime from the
    settings API, so all access to self.client/self.config is lock-guarded."""

    def __init__(self, config: MqttConfig):
        self._lock = threading.RLock()
        self.config = config
        self.client = _build_client(config)

    def connect(self) -> None:
        with self._lock:
            logger.info(
                "connecting to MQTT broker %s:%d", self.config.host, self.config.port
            )
            self.client.connect(self.config.host, self.config.port)
            self.client.loop_start()

    def reconfigure(self, new_config: MqttConfig) -> None:
        with self._lock:
            logger.info(
                "reconfiguring MQTT broker -> %s:%d", new_config.host, new_config.port
            )
            old_client = self.client
            try:
                old_client.loop_stop()
                old_client.disconnect()
            except Exception:
                logger.exception("error disconnecting previous MQTT client")

            self.client = _build_client(new_config)
            self.config = new_config
            self.client.connect(self.config.host, self.config.port)
            self.client.loop_start()

    def publish(self, event: DetectionEvent) -> None:
        with self._lock:
            client = self.client
            topic = self.config.topic.format(source=event.source, label=event.label)

        payload = json.dumps(
            {
                "source": event.source,
                "label": event.label,
                "score": round(event.score, 3),
                "timestamp": event.timestamp,
                "file": event.file,
                "duration": round(event.duration, 2),
            }
        )
        result = client.publish(topic, payload, qos=1)
        result.wait_for_publish(timeout=5)
        logger.info("published to %s: %s", topic, payload)

    def publish_status(self, source: str, status: str) -> None:
        """status is 'connecting' | 'connected' | 'disconnected'. Retained so
        a subscriber connecting later immediately sees current state, the
        same convention as an MQTT availability topic."""
        with self._lock:
            client = self.client
            topic = self.config.status_topic.format(source=source)

        payload = json.dumps({"source": source, "status": status, "timestamp": time.time()})
        result = client.publish(topic, payload, qos=1, retain=True)
        result.wait_for_publish(timeout=5)
        logger.info("published status to %s: %s", topic, payload)

    def publish_space_alert(self, used_bytes: int, max_bytes: int, deleted_count: int) -> None:
        with self._lock:
            client = self.client
            topic = self.config.system_topic.format(event="space_limit_reached")

        payload = json.dumps(
            {
                "used_bytes": used_bytes,
                "max_bytes": max_bytes,
                "deleted_count": deleted_count,
                "timestamp": time.time(),
            }
        )
        result = client.publish(topic, payload, qos=1)
        result.wait_for_publish(timeout=5)
        logger.info("published space alert to %s: %s", topic, payload)

    def stop(self) -> None:
        with self._lock:
            self.client.loop_stop()
            self.client.disconnect()
