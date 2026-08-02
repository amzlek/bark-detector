"""Load and validate the YAML config file."""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import tempfile
from dataclasses import dataclass, field
from typing import TypeVar

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


_T = TypeVar("_T", bool, int, float, str)


def _env_override(key: str, current: _T) -> _T:
    """ENV always wins over the config file. Casts the env var's string
    value to match `current`'s type - so this only works for fields that
    are never None; see _env_override_optional_float for nullable numerics."""
    raw = os.environ.get(key)
    if raw is None:
        return current
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")  # type: ignore[return-value]
    if isinstance(current, int):
        return int(raw)  # type: ignore[return-value]
    if isinstance(current, float):
        return float(raw)  # type: ignore[return-value]
    return raw  # type: ignore[return-value]


def _env_override_optional_str(key: str, current: str | None) -> str | None:
    """Like _env_override, but for nullable string fields (username/
    password) where `current` may already be None - constrained TypeVars
    can't include None as an option."""
    raw = os.environ.get(key)
    return current if raw is None else raw


def _env_override_optional_float(key: str, current: float | None) -> float | None:
    """Like _env_override, but for nullable numeric fields (e.g. cleanup
    limits) where `current` may already be None - so the cast can't be
    inferred from it. "none"/"null"/"" (case-insensitive) disables the
    limit; anything else is parsed as a float."""
    raw = os.environ.get(key)
    if raw is None:
        return current
    if raw.strip().lower() in ("none", "null", ""):
        return None
    return float(raw)


@dataclass
class MqttConfig:
    # off by default: a fresh boot with no config shouldn't try to reach a
    # broker that was never configured - turn it on via config.yaml, the
    # settings UI, or MQTT_ENABLED=true once you have a real broker
    enabled: bool = False
    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    # base prefix - event/status/system topics are derived from it below,
    # not independently configurable (that was more knobs than anyone
    # actually needed)
    topic: str = "bark_detector"
    client_id: str = "bark_detector"

    @property
    def event_topic(self) -> str:
        """{source} placeholder."""
        return f"{self.topic}/{{source}}/event"

    @property
    def status_topic(self) -> str:
        """{source} placeholder; published (retained) on every connected/
        disconnected transition, so a fresh subscriber immediately sees
        current state."""
        return f"{self.topic}/{{source}}/status"

    @property
    def system_topic(self) -> str:
        """{event} placeholder; used for cross-source notifications like
        the storage cleanup hitting its space cap."""
        return f"{self.topic}/system/{{event}}"


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
    # gates the websocket (settings CRUD + live status) - the browser only
    # learns it because index.html/settings.html render it in server-side,
    # so a third-party page can't open a websocket to us even though the
    # plain GET routes stay open. Empty means "not generated yet"; see
    # _build_web_config, which fills in a random one and main.py's
    # self-heal write-back persists it.
    auth_token: str = ""


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
    return MqttConfig(
        enabled=_env_override("MQTT_ENABLED", bool(raw.get("enabled", MqttConfig.enabled))),
        host=_env_override("MQTT_HOST", raw.get("host", MqttConfig.host)),
        port=_env_override("MQTT_PORT", int(raw.get("port", MqttConfig.port))),
        username=_env_override_optional_str("MQTT_USERNAME", raw.get("username")),
        password=_env_override_optional_str("MQTT_PASSWORD", raw.get("password")),
        topic=_env_override("MQTT_TOPIC", raw.get("topic", MqttConfig.topic)),
        client_id=_env_override("MQTT_CLIENT_ID", raw.get("client_id", MqttConfig.client_id)),
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
    token = _env_override_optional_str("WEB_AUTH_TOKEN", raw.get("auth_token"))
    if not token:
        token = secrets.token_urlsafe(32)
        logger.info(
            "generated a new websocket auth token (none was configured) - "
            "it'll be written back to config.yaml"
        )

    return WebConfig(
        enabled=_env_override("WEB_ENABLED", bool(raw.get("enabled", WebConfig.enabled))),
        host=_env_override("WEB_HOST", raw.get("host", WebConfig.host)),
        port=_env_override("WEB_PORT", int(raw.get("port", WebConfig.port))),
        auth_token=token,
    )


def _build_cleanup_config(raw: dict) -> CleanupConfig:
    def _optional_float(key: str, default: float | None) -> float | None:
        if key not in raw:
            return default
        value = raw[key]
        return None if value is None else float(value)

    return CleanupConfig(
        max_age_days=_env_override_optional_float(
            "CLEANUP_MAX_AGE_DAYS", _optional_float("max_age_days", CleanupConfig.max_age_days)
        ),
        max_space_mb=_env_override_optional_float(
            "CLEANUP_MAX_SPACE_MB", _optional_float("max_space_mb", CleanupConfig.max_space_mb)
        ),
        check_interval_minutes=_env_override(
            "CLEANUP_CHECK_INTERVAL_MINUTES",
            float(raw.get("check_interval_minutes", CleanupConfig.check_interval_minutes)),
        ),
    )


def load_config(path: str) -> Config:
    """Tolerant by design: a missing file, an empty file, or a file missing
    whole sections are all fine - every gap is filled with its default (or
    an env var override, which always wins regardless of what's in the
    file). Only genuinely malformed values (e.g. an unknown source type,
    a duplicate source name) still raise ConfigError. Callers that want the
    resolved config persisted back to disk (so a sparse/missing file becomes
    a fully populated one) should follow up with save_config() - this
    function itself never writes, so it stays safe to call on arbitrary
    paths (tests, an example file, etc.) without side effects."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError:
        raw = {}

    defaults = raw.get("defaults", {})
    names = set()
    sources = []
    for raw_source in raw.get("sources") or []:
        source = build_source_config(raw_source, defaults)
        if source.name in names:
            raise ConfigError(f"duplicate source name '{source.name}'")
        names.add(source.name)
        sources.append(source)

    return Config(
        mqtt=build_mqtt_config(raw.get("mqtt", {})),
        sources=sources,
        snippet_dir=_env_override("SNIPPET_DIR", raw.get("snippet_dir", Config.snippet_dir)),
        ffmpeg_path=_env_override("FFMPEG_PATH", raw.get("ffmpeg_path", Config.ffmpeg_path)),
        log_level=_env_override("LOG_LEVEL", raw.get("log_level", Config.log_level)),
        web=_build_web_config(raw.get("web", {})),
        cleanup=_build_cleanup_config(raw.get("cleanup", {})),
    )


def dump_config(config: Config) -> dict:
    """Serialize a Config back to the same shape load_config() reads.
    No 'defaults' section is written back - every source is fully explicit."""
    return {
        "mqtt": {
            "enabled": config.mqtt.enabled,
            "host": config.mqtt.host,
            "port": config.mqtt.port,
            "username": config.mqtt.username,
            "password": config.mqtt.password,
            "topic": config.mqtt.topic,
            "client_id": config.mqtt.client_id,
        },
        "snippet_dir": config.snippet_dir,
        "ffmpeg_path": config.ffmpeg_path,
        "log_level": config.log_level,
        "web": {
            "enabled": config.web.enabled,
            "host": config.web.host,
            "port": config.web.port,
            "auth_token": config.web.auth_token,
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
