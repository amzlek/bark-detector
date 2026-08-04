"""Publishes detection events to MQTT as JSON metadata + a snippet file path."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from .config import MqttConfig
from .snippet import DetectionEvent

logger = logging.getLogger(__name__)


class MqttPublisher:
    """Thread-safe: publish() is called concurrently from every SourceWorker,
    and reconfigure() may swap the broker connection at runtime from the
    settings API, so all access to self.client/self.config is lock-guarded.

    A disabled config (mqtt.enabled: false) makes every method a no-op -
    callers (controller.py, worker.py, cleanup.py) don't need to know or
    care whether MQTT is turned on."""

    def __init__(self, config: MqttConfig, on_status_change: Callable[[bool], None] | None = None):
        self._lock = threading.RLock()
        self.config = config
        self.connected = False
        # notified (outside the lock) whenever the broker connection flips -
        # the websocket layer uses this to push live status to the UI
        self.on_status_change = on_status_change
        self.client = self._build_client(config)

    def _build_client(self, config: MqttConfig) -> mqtt.Client:
        client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=config.client_id)
        if config.username:
            client.username_pw_set(config.username, config.password)
        client.on_connect = self._handle_connect
        client.on_disconnect = self._handle_disconnect
        return client

    def _handle_connect(self, client, userdata, *args) -> None:
        self._set_connected(True)

    def _handle_disconnect(self, client, userdata, *args) -> None:
        self._set_connected(False)

    def _set_connected(self, connected: bool) -> None:
        with self._lock:
            changed = connected != self.connected
            self.connected = connected
        if changed and self.on_status_change:
            try:
                self.on_status_change(connected)
            except Exception:
                logger.exception("mqtt status change callback failed")

    def is_connected(self) -> bool:
        with self._lock:
            return self.connected

    def connect(self) -> None:
        with self._lock:
            if not self.config.enabled:
                logger.info("MQTT disabled, not connecting")
                return
            logger.info(
                "connecting to MQTT broker %s:%d", self.config.host, self.config.port
            )
            self.client.connect_async(self.config.host, self.config.port)
            self.client.loop_start()

    def reconfigure(self, new_config: MqttConfig) -> None:
        with self._lock:
            old_client = self.client
            try:
                old_client.loop_stop()
                old_client.disconnect()
            except Exception:
                logger.exception("error disconnecting previous MQTT client")

            self.client = self._build_client(new_config)
            self.config = new_config

        self._set_connected(False)

        with self._lock:
            if not new_config.enabled:
                logger.info("MQTT reconfigured -> disabled")
                return

            logger.info(
                "reconfiguring MQTT broker -> %s:%d", new_config.host, new_config.port
            )
            self.client.connect_async(self.config.host, self.config.port)
            self.client.loop_start()

    def publish(self, event: DetectionEvent) -> None:
        with self._lock:
            if not self.config.enabled:
                return
            client = self.client
            topic = self.config.event_topic.format(source=event.source, label=event.label)

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
            if not self.config.enabled:
                return
            client = self.client
            topic = self.config.status_topic.format(source=source)

        payload = json.dumps({"source": source, "status": status, "timestamp": time.time()})
        result = client.publish(topic, payload, qos=1, retain=True)
        result.wait_for_publish(timeout=5)
        logger.info("published status to %s: %s", topic, payload)

    def publish_space_alert(self, used_bytes: int, max_bytes: int, deleted_count: int) -> None:
        with self._lock:
            if not self.config.enabled:
                return
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
            if not self.config.enabled:
                return
            self.client.loop_stop()
            self.client.disconnect()
        self._set_connected(False)
