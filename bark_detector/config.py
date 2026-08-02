"""Load and validate the YAML config file."""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from dataclasses import dataclass, field

import yaml

logger = logging.getLogger(__name__)

DEFAULT_LISTEN = ["bark", "bow-wow", "howl", "yip"]
DEFAULT_THRESHOLD = 0.8
DEFAULT_MIN_VOLUME = 500
DEFAULT_PRE_CAPTURE = 5.0
DEFAULT_POST_CAPTURE = 5.0
VALID_SOURCE_TYPES = ("rtsp", "device", "wyoming")


class ConfigError(ValueError):
    pass


@dataclass
class MqttConfig:
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    topic: str = "bark_detector/{source}/event"
    # {source} placeholder; published (retained) on every connected/disconnected
    # transition, so a fresh subscriber immediately sees current state
    status_topic: str = "bark_detector/{source}/status"
    # {event} placeholder; used for cross-source notifications like the
    # storage cleanup hitting its space cap
    system_topic: str = "bark_detector/system/{event}"
    client_id: str = "bark_detector"


@dataclass
class SourceConfig:
    name: str
    type: str
    path: str
    listen: list[str] = field(default_factory=lambda: list(DEFAULT_LISTEN))
    thresholds: dict[str, float] = field(default_factory=dict)
    min_volume: float = DEFAULT_MIN_VOLUME
    pre_capture: float = DEFAULT_PRE_CAPTURE
    post_capture: float = DEFAULT_POST_CAPTURE
    input_args: list[str] = field(default_factory=list)

    def threshold_for(self, label: str) -> float:
        return self.thresholds.get(label, DEFAULT_THRESHOLD)


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8099


@dataclass
class CleanupConfig:
    # any of these can be set to null/None in YAML to disable that limit
    max_age_days: float | None = 30.0
    max_space_mb: float | None = 1000.0
    check_interval_minutes: float = 15.0


@dataclass
class Config:
    mqtt: MqttConfig
    sources: list[SourceConfig]
    snippet_dir: str = "/media/bark_snippets"
    ffmpeg_path: str = "ffmpeg"
    log_level: str = "INFO"
    web: WebConfig = field(default_factory=WebConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)

    @property
    def db_path(self) -> str:
        return os.path.join(self.snippet_dir, "events.db")


def build_mqtt_config(raw: dict) -> MqttConfig:
    if "host" not in raw:
        raise ConfigError("mqtt.host is required")
    return MqttConfig(
        host=raw["host"],
        port=int(raw.get("port", 1883)),
        username=raw.get("username"),
        password=raw.get("password"),
        topic=raw.get("topic", MqttConfig.topic),
        status_topic=raw.get("status_topic", MqttConfig.status_topic),
        system_topic=raw.get("system_topic", MqttConfig.system_topic),
        client_id=raw.get("client_id", MqttConfig.client_id),
    )


def build_source_config(raw: dict, defaults: dict | None = None) -> SourceConfig:
    defaults = defaults or {}

    for required in ("name", "type", "path"):
        if required not in raw:
            raise ConfigError(f"source is missing required field '{required}': {raw}")

    source_type = raw["type"]
    if source_type not in VALID_SOURCE_TYPES:
        raise ConfigError(
            f"source '{raw['name']}' has invalid type '{source_type}', "
            f"must be one of {VALID_SOURCE_TYPES}"
        )

    if source_type == "wyoming":
        raise ConfigError(
            f"source '{raw['name']}': the 'wyoming' source type "
            "(e.g. for an M5 Atom Echo) is not implemented yet"
        )

    return SourceConfig(
        name=raw["name"],
        type=source_type,
        path=raw["path"],
        listen=raw.get("listen", defaults.get("listen", list(DEFAULT_LISTEN))),
        thresholds=raw.get("thresholds", defaults.get("thresholds", {})),
        min_volume=float(
            raw.get("min_volume", defaults.get("min_volume", DEFAULT_MIN_VOLUME))
        ),
        pre_capture=float(
            raw.get("pre_capture", defaults.get("pre_capture", DEFAULT_PRE_CAPTURE))
        ),
        post_capture=float(
            raw.get("post_capture", defaults.get("post_capture", DEFAULT_POST_CAPTURE))
        ),
        input_args=raw.get("input_args", []),
    )


def _build_web_config(raw: dict) -> WebConfig:
    return WebConfig(
        enabled=bool(raw.get("enabled", WebConfig.enabled)),
        host=raw.get("host", WebConfig.host),
        port=int(raw.get("port", WebConfig.port)),
    )


def _build_cleanup_config(raw: dict) -> CleanupConfig:
    def _optional_float(key: str, default: float | None) -> float | None:
        if key not in raw:
            return default
        value = raw[key]
        return None if value is None else float(value)

    return CleanupConfig(
        max_age_days=_optional_float("max_age_days", CleanupConfig.max_age_days),
        max_space_mb=_optional_float("max_space_mb", CleanupConfig.max_space_mb),
        check_interval_minutes=float(
            raw.get("check_interval_minutes", CleanupConfig.check_interval_minutes)
        ),
    )


def load_config(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if "mqtt" not in raw:
        raise ConfigError("top-level 'mqtt' section is required")
    if not raw.get("sources"):
        raise ConfigError("at least one entry in top-level 'sources' is required")

    defaults = raw.get("defaults", {})
    names = set()
    sources = []
    for raw_source in raw["sources"]:
        source = build_source_config(raw_source, defaults)
        if source.name in names:
            raise ConfigError(f"duplicate source name '{source.name}'")
        names.add(source.name)
        sources.append(source)

    return Config(
        mqtt=build_mqtt_config(raw["mqtt"]),
        sources=sources,
        snippet_dir=raw.get("snippet_dir", Config.snippet_dir),
        ffmpeg_path=raw.get("ffmpeg_path", Config.ffmpeg_path),
        log_level=raw.get("log_level", Config.log_level),
        web=_build_web_config(raw.get("web", {})),
        cleanup=_build_cleanup_config(raw.get("cleanup", {})),
    )


def dump_config(config: Config) -> dict:
    """Serialize a Config back to the same shape load_config() reads.
    No 'defaults' section is written back - every source is fully explicit."""
    return {
        "mqtt": {
            "host": config.mqtt.host,
            "port": config.mqtt.port,
            "username": config.mqtt.username,
            "password": config.mqtt.password,
            "topic": config.mqtt.topic,
            "status_topic": config.mqtt.status_topic,
            "system_topic": config.mqtt.system_topic,
            "client_id": config.mqtt.client_id,
        },
        "snippet_dir": config.snippet_dir,
        "ffmpeg_path": config.ffmpeg_path,
        "log_level": config.log_level,
        "web": {
            "enabled": config.web.enabled,
            "host": config.web.host,
            "port": config.web.port,
        },
        "cleanup": {
            "max_age_days": config.cleanup.max_age_days,
            "max_space_mb": config.cleanup.max_space_mb,
            "check_interval_minutes": config.cleanup.check_interval_minutes,
        },
        "sources": [
            {
                "name": s.name,
                "type": s.type,
                "path": s.path,
                "listen": s.listen,
                "thresholds": s.thresholds,
                "min_volume": s.min_volume,
                "pre_capture": s.pre_capture,
                "post_capture": s.post_capture,
                "input_args": s.input_args,
            }
            for s in config.sources
        ],
    }


def save_config(config: Config, path: str) -> None:
    """Write the config back to disk atomically (write to a temp file in the
    same directory, then rename) so a crash mid-write can't corrupt it."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(dump_config(config), f, sort_keys=False)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise
