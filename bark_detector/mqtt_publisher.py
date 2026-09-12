"""Publishes detection events to MQTT as JSON metadata + a snippet file path."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from . import ha_discovery
from .config import MqttConfig, SourceConfig
from .snippet import DetectionEvent, DetectionTrigger

logger = logging.getLogger(__name__)

MAX_QUEUED_MESSAGES = 1000


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
        # Paho retains QoS 1 messages awaiting PUBACK. Bound that backlog
        # while preserving submission order; a full queue drops the newest.
        client.max_queued_messages_set(MAX_QUEUED_MESSAGES)
        if config.username:
            client.username_pw_set(config.username, config.password)
        client.on_connect = self._handle_connect
        client.on_disconnect = self._handle_disconnect
        # Last Will: if the process crashes or loses the broker
        # ungracefully, the broker publishes this on our behalf so
        # subscribers (HA included) see "offline" instead of a stale last
        # value forever. Mirrored by an explicit "online" publish in
        # _handle_connect.
        client.will_set(config.availability_topic, "offline", qos=1, retain=True)
        return client

    def _handle_connect(self, client, userdata, *args) -> None:
        self._set_connected(True)
        with self._lock:
            if not self.config.enabled:
                return
            topic = self.config.availability_topic
        # Paho receives the PUBACK on this same network-loop thread.
        client.publish(topic, "online", qos=1, retain=True)

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

    @staticmethod
    def _publish(client, topic: str, payload, *, retain: bool = False) -> None:
        result = client.publish(topic, payload, qos=1, retain=retain)
        if result.rc == mqtt.MQTT_ERR_QUEUE_SIZE:
            logger.warning("MQTT queue full; dropped newest message for %s", topic)
        elif result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.warning("MQTT publish failed for %s: %s", topic, result.rc)

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

    def publish_trigger(self, trigger: DetectionTrigger) -> None:
        """Fired the instant a bark crosses threshold, before the snippet
        has finished recording - so an automation can react immediately
        instead of waiting out post_capture. publish_event() follows once
        the snippet is saved, carrying the same "id" for correlation."""
        with self._lock:
            if not self.config.enabled:
                return
            client = self.client
            topic = self.config.triggered_topic.format(source=trigger.source)

        payload = json.dumps(
            {
                "id": trigger.id,
                "source": trigger.source,
                "label": trigger.label,
                "score": round(trigger.score, 3),
                "timestamp": trigger.timestamp,
            }
        )
        self._publish(client, topic, payload)
        logger.info("published trigger to %s: %s", topic, payload)

    def publish_event(self, event: DetectionEvent) -> None:
        with self._lock:
            if not self.config.enabled:
                return
            client = self.client
            topic = self.config.event_topic.format(source=event.source)

        payload = json.dumps(
            {
                "id": event.id,
                "source": event.source,
                "label": event.label,
                "score": round(event.score, 3),
                "timestamp": event.timestamp,
                "file": event.file,
                "duration": round(event.duration, 2),
            }
        )
        self._publish(client, topic, payload)
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
        self._publish(client, topic, payload, retain=True)
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
        self._publish(client, topic, payload)
        logger.info("published space alert to %s: %s", topic, payload)

    def publish_discovery_for_source(self, source: SourceConfig) -> None:
        """Publishes (retained) the HA discovery configs for one source's
        entities - see ha_discovery.build_source_discovery. Called whenever
        a source is added/renamed/edited, and for every source on every
        (re)connect (see publish_all_discovery) so a fresh HA install picks
        them up without needing bark-detector restarted."""
        with self._lock:
            if not self.config.enabled or not self.config.discovery:
                return
            client = self.client
            payloads = ha_discovery.build_source_discovery(self.config, source)

        for topic, payload in payloads.items():
            self._publish(client, topic, json.dumps(payload), retain=True)
        logger.info("published HA discovery config for source '%s'", source.name)

    def remove_discovery_for_source(self, source_id: str) -> None:
        """Clears a deleted source's HA entities (the HA convention for
        removing a discovered entity is an empty retained payload on its
        config topic) - not gated on config.discovery, since a source
        deleted after discovery was turned off could still have entities
        left over from when it was on."""
        with self._lock:
            if not self.config.enabled:
                return
            client = self.client
            topics = ha_discovery.discovery_topics_for_source(self.config.discovery_prefix, source_id)

        for topic in topics:
            self._publish(client, topic, "", retain=True)
        logger.info("removed HA discovery config for source id '%s'", source_id)

    def publish_system_discovery(self) -> None:
        with self._lock:
            if not self.config.enabled or not self.config.discovery:
                return
            client = self.client
            payloads = ha_discovery.build_system_discovery(self.config)

        for topic, payload in payloads.items():
            self._publish(client, topic, json.dumps(payload), retain=True)
        logger.info("published HA discovery config for system entities")

    def remove_system_discovery(self) -> None:
        """See remove_discovery_for_source - same "clean up on delete/
        disable" convention, for the one instance-wide entity."""
        with self._lock:
            if not self.config.enabled:
                return
            client = self.client
            topic = ha_discovery.system_discovery_topic(self.config.discovery_prefix)

        self._publish(client, topic, "", retain=True)
        logger.info("removed HA discovery config for system entities")

    def publish_all_discovery(self, sources: list[SourceConfig]) -> None:
        """Called on every successful (re)connect - retained publishes are
        cheap no-ops from HA's perspective when nothing changed, and this is
        the only way a fresh HA instance/broker (no retained messages yet)
        ends up with the full set of entities without a bark-detector
        restart."""
        self.publish_system_discovery()
        for source in sources:
            self.publish_discovery_for_source(source)

    def stop(self) -> None:
        with self._lock:
            if not self.config.enabled:
                return
            client = self.client
            topic = self.config.availability_topic
        # a clean disconnect() (unlike a crash) does NOT trigger the broker
        # to send our LWT, so publish "offline" ourselves first - otherwise
        # HA would keep showing the last state as available on a graceful
        # shutdown, not just a crash
        result = client.publish(topic, "offline", qos=1, retain=True)
        result.wait_for_publish(timeout=5)
        with self._lock:
            self.client.loop_stop()
            self.client.disconnect()
        self._set_connected(False)
