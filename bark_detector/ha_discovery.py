"""Builds Home Assistant MQTT discovery config payloads (retained messages
under `{discovery_prefix}/.../config` that make entities auto-appear in HA,
see https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery).

Pure functions only - no MQTT client dependency here, see MqttPublisher for
the thin publish/remove wrappers that actually put these on the wire."""

from __future__ import annotations

from .config import DEFAULT_LISTEN, MqttConfig, SourceConfig

# identifies the instance-level "hub" device that per-source devices link to
# via `via_device`, and that owns instance-wide (not per-source) entities.
HUB_DEVICE_ID = "bark_detector"


def _device_id(source_id: str) -> str:
    return f"bark_detector_{source_id}"


def discovery_topics_for_source(discovery_prefix: str, source_id: str) -> list[str]:
    """Config topics for one source's entities - built from source.id alone
    (not name), so a source can be located and removed (see
    MqttPublisher.remove_discovery_for_source) without needing its current
    SourceConfig, e.g. after it's already been deleted."""
    device_id = _device_id(source_id)
    return [
        f"{discovery_prefix}/event/{device_id}/detected/config",
        f"{discovery_prefix}/binary_sensor/{device_id}/connectivity/config",
    ]


def build_source_discovery(mqtt: MqttConfig, source: SourceConfig) -> dict[str, dict]:
    """topic -> discovery payload for one source's "detected" and
    "connectivity" entities, grouped under one HA device per source."""
    device_id = _device_id(source.id)
    device = {
        "identifiers": [device_id],
        "name": source.name,
        "via_device": HUB_DEVICE_ID,
    }
    detected_topic, connectivity_topic = discovery_topics_for_source(
        mqtt.discovery_prefix, source.id
    )

    return {
        detected_topic: {
            "name": "Detected",
            "unique_id": f"{device_id}_detected",
            # fires the instant a bark crosses threshold (see
            # triggered_topic) - json_attributes_topic fills in score/file/
            # duration shortly after, once the snippet is saved
            "state_topic": mqtt.triggered_topic.format(source=source.name),
            "event_types": list(source.listen) or list(DEFAULT_LISTEN),
            "value_template": "{{ {'event_type': value_json.label} | tojson }}",
            "json_attributes_topic": mqtt.event_topic.format(source=source.name),
            "availability_topic": mqtt.availability_topic,
            "device": device,
        },
        connectivity_topic: {
            "name": "Connectivity",
            "unique_id": f"{device_id}_connectivity",
            "device_class": "connectivity",
            "state_topic": mqtt.status_topic.format(source=source.name),
            "value_template": "{{ 'ON' if value_json.status == 'connected' else 'OFF' }}",
            "availability_topic": mqtt.availability_topic,
            "device": device,
        },
    }


def system_discovery_topic(discovery_prefix: str) -> str:
    return f"{discovery_prefix}/event/{HUB_DEVICE_ID}/space_alert/config"


def build_system_discovery(mqtt: MqttConfig) -> dict[str, dict]:
    """topic -> discovery payload for the instance-wide diagnostic entity
    that surfaces the storage cleanup's space-limit alert in HA."""
    topic = system_discovery_topic(mqtt.discovery_prefix)
    return {
        topic: {
            "name": "Space Alert",
            "unique_id": f"{HUB_DEVICE_ID}_space_alert",
            "state_topic": mqtt.system_topic.format(event="space_limit_reached"),
            "event_types": ["space_limit_reached"],
            "value_template": "{{ {'event_type': 'space_limit_reached'} | tojson }}",
            "json_attributes_topic": mqtt.system_topic.format(event="space_limit_reached"),
            "entity_category": "diagnostic",
            "availability_topic": mqtt.availability_topic,
            "device": {"identifiers": [HUB_DEVICE_ID], "name": "Bark Detector"},
        }
    }
