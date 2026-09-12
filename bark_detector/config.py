"""Load and validate the YAML config file."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import TypeVar

import yaml

logger = logging.getLogger(__name__)

DEFAULT_LISTEN = ["bark", "bow-wow", "howl", "yip"]
DEFAULT_THRESHOLD = 0.8
DEFAULT_MIN_VOLUME = 500
DEFAULT_PRE_CAPTURE = 5.0
DEFAULT_POST_CAPTURE = 5.0
VALID_SOURCE_TYPES = ("rtsp", "device", "esphome_tcp")

# sentinel: "leave this key out of the persisted file" - see _persisted_value
_OMIT = object()


class ConfigError(ValueError):
    pass


_T = TypeVar("_T", bool, int, float, str)


def _env_override(key: str, current: _T, *, in_file: bool = False) -> _T:
    """ENV always wins over the config file. Casts the env var's string
    value to match `current`'s type - so this only works for fields that
    are never None; see _env_override_optional_float for nullable numerics.

    in_file=True means the config file explicitly set this value - if the
    env var is ALSO set, that file value is about to be silently ignored,
    which is worth a warning since it's easy to forget an env var is still
    in scope after editing the file and wonder why the edit didn't stick."""
    raw = os.environ.get(key)
    if raw is None:
        return current
    if in_file:
        logger.warning("%s is set in the config file, but env var %s overrides it", key.lower(), key)
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")  # type: ignore[return-value]
    if isinstance(current, int):
        return int(raw)  # type: ignore[return-value]
    if isinstance(current, float):
        return float(raw)  # type: ignore[return-value]
    return raw  # type: ignore[return-value]


def _env_override_optional_str(
    key: str, current: str | None, *, in_file: bool = False
) -> str | None:
    """Like _env_override, but for nullable string fields (username/
    password) where `current` may already be None - constrained TypeVars
    can't include None as an option."""
    raw = os.environ.get(key)
    if raw is None:
        return current
    if in_file:
        logger.warning("%s is set in the config file, but env var %s overrides it", key.lower(), key)
    return raw


def _env_override_optional_float(
    key: str, current: float | None, *, in_file: bool = False
) -> float | None:
    """Like _env_override, but for nullable numeric fields (e.g. cleanup
    limits) where `current` may already be None - so the cast can't be
    inferred from it. "none"/"null"/"" (case-insensitive) disables the
    limit; anything else is parsed as a float."""
    raw = os.environ.get(key)
    if raw is None:
        return current
    if in_file:
        logger.warning("%s is set in the config file, but env var %s overrides it", key.lower(), key)
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
    # publish Home Assistant MQTT discovery configs (see ha_discovery.py) -
    # on by default so turning mqtt on "just works" with HA; the raw topics
    # below are published either way, this only adds the extra retained
    # homeassistant/.../config messages that make entities auto-appear
    discovery: bool = True
    discovery_prefix: str = "homeassistant"

    @property
    def availability_topic(self) -> str:
        """Whole-process liveness (LWT-backed - see MqttPublisher._build_client),
        distinct from the per-source status_topic below, which tracks
        whether audio is actually flowing on one source, not whether the
        process/broker connection itself is alive."""
        return f"{self.topic}/bridge/status"

    @property
    def triggered_topic(self) -> str:
        """{source} placeholder; published the instant a bark crosses
        threshold, before the snippet has finished recording - see
        event_topic, which follows once the snippet is saved and carries
        the same "id" for correlation."""
        return f"{self.topic}/{{source}}/triggered"

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
    # stable identity that survives a rename (unlike `name`, which is used
    # as-is in MQTT topics and is freely user-editable) - generated once
    # here and carried forward by build_source_config whenever a caller
    # passes an existing "id" through, same "chosen once, never recomputed"
    # rule as _slugify's on-disk filename. Used as the HA discovery
    # unique_id/device identifier, so renaming a source updates its
    # existing HA entity instead of creating a duplicate.
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

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
    max_space_mb: float | None = 100.0
    check_interval_minutes: float = 15.0


@dataclass
class Config:
    mqtt: MqttConfig
    sources: list[SourceConfig]
    # where load_sources()/save_source() read/write the one-file-per-source
    # directory, and where the settings UI persists add/edit/delete - a
    # real field (not just a load_config() parameter) so callers can read
    # back the fully-resolved value (file/env/default) after loading,
    # rather than needing to duplicate that resolution themselves
    sources_dir: str = "/config/sources"
    # applied when a *.yaml file under sources_dir doesn't specify a field -
    # lives here (not in a per-source file) since it's a policy shared
    # across sources, not data belonging to any one of them. Never applied
    # to sources added/edited through the settings UI, which always send
    # every field explicit already.
    source_defaults: dict = field(default_factory=dict)
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
        enabled=_env_override(
            "MQTT_ENABLED", bool(raw.get("enabled", MqttConfig.enabled)), in_file="enabled" in raw
        ),
        host=_env_override("MQTT_HOST", raw.get("host", MqttConfig.host), in_file="host" in raw),
        port=_env_override(
            "MQTT_PORT", int(raw.get("port", MqttConfig.port)), in_file="port" in raw
        ),
        # "or None" normalizes an empty string (e.g. a blank form field) to
        # None, matching the dataclass default - otherwise "" vs None would
        # be treated as different values by dump_config's default check
        username=_env_override_optional_str(
            "MQTT_USERNAME", raw.get("username") or None, in_file="username" in raw
        ),
        password=_env_override_optional_str(
            "MQTT_PASSWORD", raw.get("password") or None, in_file="password" in raw
        ),
        topic=_env_override("MQTT_TOPIC", raw.get("topic", MqttConfig.topic), in_file="topic" in raw),
        client_id=_env_override(
            "MQTT_CLIENT_ID", raw.get("client_id", MqttConfig.client_id), in_file="client_id" in raw
        ),
        discovery=_env_override(
            "MQTT_DISCOVERY", bool(raw.get("discovery", MqttConfig.discovery)), in_file="discovery" in raw
        ),
        discovery_prefix=_env_override(
            "MQTT_DISCOVERY_PREFIX",
            raw.get("discovery_prefix", MqttConfig.discovery_prefix),
            in_file="discovery_prefix" in raw,
        ),
    )


def build_source_config(raw: dict, defaults: dict | None = None) -> SourceConfig:
    defaults = defaults or {}

    for required in ("name", "type", "path"):
        if required not in raw:
            raise ConfigError(f"source is missing required field '{required}': {raw}")

    name = raw["name"]
    if not name or name != name.strip():
        raise ConfigError(f"source name {name!r} must be non-empty with no leading/trailing whitespace")
    if "/" in name:
        raise ConfigError(f"source name {name!r} can't contain '/' (it's used as an MQTT topic segment)")
    if any(ord(c) < 0x20 for c in name):
        raise ConfigError(f"source name {name!r} can't contain control characters")

    source_type = raw["type"]
    if source_type not in VALID_SOURCE_TYPES:
        raise ConfigError(
            f"source '{name}' has invalid type '{source_type}', "
            f"must be one of {VALID_SOURCE_TYPES}"
        )

    # only pass "id" through when the caller has one to preserve (an
    # existing source being reloaded/renamed) - omitting it lets
    # SourceConfig's default_factory mint a fresh one for a brand new source
    id_kwargs: dict[str, str] = {"id": raw["id"]} if raw.get("id") else {}

    return SourceConfig(
        name=name,
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
        **id_kwargs,
    )


def _build_web_config(raw: dict) -> WebConfig:
    return WebConfig(
        enabled=_env_override(
            "WEB_ENABLED", bool(raw.get("enabled", WebConfig.enabled)), in_file="enabled" in raw
        ),
        host=_env_override("WEB_HOST", raw.get("host", WebConfig.host), in_file="host" in raw),
        port=_env_override(
            "WEB_PORT", int(raw.get("port", WebConfig.port)), in_file="port" in raw
        ),
    )


def _build_cleanup_config(raw: dict) -> CleanupConfig:
    def _optional_float(key: str, default: float | None) -> float | None:
        if key not in raw:
            return default
        value = raw[key]
        return None if value is None else float(value)

    return CleanupConfig(
        max_age_days=_env_override_optional_float(
            "CLEANUP_MAX_AGE_DAYS",
            _optional_float("max_age_days", CleanupConfig.max_age_days),
            in_file="max_age_days" in raw,
        ),
        max_space_mb=_env_override_optional_float(
            "CLEANUP_MAX_SPACE_MB",
            _optional_float("max_space_mb", CleanupConfig.max_space_mb),
            in_file="max_space_mb" in raw,
        ),
        check_interval_minutes=_env_override(
            "CLEANUP_CHECK_INTERVAL_MINUTES",
            float(raw.get("check_interval_minutes", CleanupConfig.check_interval_minutes)),
            in_file="check_interval_minutes" in raw,
        ),
    )


def load_sources(sources_dir: str, defaults: dict | None = None) -> list[SourceConfig]:
    """One *.yaml file per source in sources_dir, each with its own explicit
    'name:' field - the filename itself is just an opaque storage detail
    (a slug picked once when the source is first saved, see save_source),
    not the source's real identity, so a name can be anything (spaces,
    punctuation, unicode - e.g. 'abc:3') without having to also be a valid
    filename. A missing directory just means zero sources."""
    if not os.path.isdir(sources_dir):
        return []

    sources = []
    seen_names: set[str] = set()
    for filename in sorted(os.listdir(sources_dir)):
        if not filename.endswith(".yaml"):
            continue
        with open(os.path.join(sources_dir, filename), "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        source = build_source_config(raw, defaults)
        if source.name in seen_names:
            raise ConfigError(f"duplicate source name '{source.name}' (in {filename})")
        seen_names.add(source.name)
        sources.append(source)
        # one-time migration: a source file saved before `id` existed - mint
        # one now and persist it immediately so it doesn't change again on
        # the next load (which would silently orphan its HA discovery entity).
        # Best-effort: sources_dir may be read-only (e.g. a mounted fixture
        # in a test rig) - losing the migration write there just means the
        # id won't survive a restart, which shouldn't take the whole app
        # down when everything else about loading this source succeeded.
        if "id" not in raw:
            try:
                save_source(source, sources_dir)
            except OSError:
                logger.warning(
                    "couldn't persist a newly generated id for source '%s' (%s is read-only?)",
                    source.name,
                    sources_dir,
                )
    return sources


def _slugify(name: str) -> str:
    """Best-effort filesystem-safe stand-in for a source's name, used only
    to pick a NEW file's name (see save_source) - never recomputed for an
    existing source, so renaming one never moves its file."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_-")
    return slug or "source"


def _find_source_path(name: str, sources_dir: str) -> str | None:
    """Filenames aren't derived from the name (see load_sources), so the
    only reliable way to find the file backing a given source is to check
    each file's actual 'name:' content."""
    if not os.path.isdir(sources_dir):
        return None
    for filename in os.listdir(sources_dir):
        if not filename.endswith(".yaml"):
            continue
        path = os.path.join(sources_dir, filename)
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        if raw.get("name") == name:
            return path
    return None


def _new_source_path(name: str, sources_dir: str) -> str:
    slug = _slugify(name)
    path = os.path.join(sources_dir, f"{slug}.yaml")
    suffix = 2
    while os.path.exists(path):
        path = os.path.join(sources_dir, f"{slug}-{suffix}.yaml")
        suffix += 1
    return path


def load_config(
    config_path: str,
    default_sources_dir: str = Config.sources_dir,
) -> Config:
    """Tolerant by design: a missing file, an empty file, or a file missing
    whole sections are all fine - every gap is filled with its default (or
    an env var override, which always wins regardless of what's in the
    file). Only genuinely malformed values (e.g. an unknown source type or
    name) still raise ConfigError. This function never writes anything -
    safe to call on arbitrary paths (tests, an example file, etc.) without
    side effects. Persisting changes is a separate, explicit step: see
    save_config/save_source, called only when the settings UI changes
    something - never automatically just because the app booted.

    Config is split between app settings and source files:
      - config_path: app settings (mqtt/web/cleanup/sources_dir/etc) -
        entirely optional, every field can come
        from an env var instead, so a docker deployment can skip mounting
        this file at all. config_path itself can't be one of those fields
        (you'd need to already know it to find the file that would tell
        you it) - it's the one thing main.py resolves before ever calling
        this function.
      - sources_dir: one *.yaml per source (see load_sources) - sources
        aren't env-var configurable themselves (no clean way to express a
        dynamic list that way), so they need somewhere to persist to
        regardless; where is configurable, same as everything else
        (SOURCES_DIR / sources_dir: in the file / default_sources_dir).
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError:
        raw = {}

    sources_dir = _env_override(
        "SOURCES_DIR", raw.get("sources_dir", default_sources_dir), in_file="sources_dir" in raw
    )
    source_defaults = raw.get("source_defaults", {})

    return Config(
        mqtt=build_mqtt_config(raw.get("mqtt", {})),
        sources=load_sources(sources_dir, source_defaults),
        sources_dir=sources_dir,
        source_defaults=source_defaults,
        snippet_dir=_env_override(
            "SNIPPET_DIR", raw.get("snippet_dir", Config.snippet_dir), in_file="snippet_dir" in raw
        ),
        ffmpeg_path=raw.get("ffmpeg_path", Config.ffmpeg_path),
        log_level=_env_override(
            "LOG_LEVEL", raw.get("log_level", Config.log_level), in_file="log_level" in raw
        ),
        web=_build_web_config(raw.get("web", {})),
        cleanup=_build_cleanup_config(raw.get("cleanup", {})),
    )


def _persisted_value(current, default, env_key: str | None):
    """Returns current if it belongs in the persisted file, or _OMIT if it
    should be left out - either because it's still at its default (no
    point cluttering the file with it; config.example.yaml documents the
    full list of what's available) or because it's currently sourced from
    an env var, which will keep winning on every future load regardless of
    what the file says - writing it back would just leak it into a
    plaintext file for no functional benefit. Matters most for secrets
    like MQTT_PASSWORD, but applied uniformly to every field for
    consistency (see dump_config)."""
    if env_key is not None and os.environ.get(env_key) is not None:
        return _OMIT
    if current == default:
        return _OMIT
    return current


def _drop_omitted(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not _OMIT}


def dump_config(config: Config) -> dict:
    """Serialize the app-level part of a Config to the shape load_config()
    reads - but only fields that differ from their default AND aren't
    currently sourced from an env var (see _persisted_value). That keeps
    the file a lean diff of your actual overrides instead of a full dump
    of every field, and guarantees a secret sourced from env never gets
    duplicated into it. Sources themselves aren't included here - each is
    its own file under sources_dir (see save_source) - but where
    sources_dir points IS included, same as any other
    setting, so a hand-set value survives an unrelated save (e.g. the
    settings UI saving MQTT config) instead of silently reverting."""
    default_mqtt = MqttConfig()
    default_web = WebConfig()
    default_cleanup = CleanupConfig()

    result = {
        "mqtt": _drop_omitted(
            {
                "enabled": _persisted_value(config.mqtt.enabled, default_mqtt.enabled, "MQTT_ENABLED"),
                "host": _persisted_value(config.mqtt.host, default_mqtt.host, "MQTT_HOST"),
                "port": _persisted_value(config.mqtt.port, default_mqtt.port, "MQTT_PORT"),
                "username": _persisted_value(
                    config.mqtt.username, default_mqtt.username, "MQTT_USERNAME"
                ),
                "password": _persisted_value(
                    config.mqtt.password, default_mqtt.password, "MQTT_PASSWORD"
                ),
                "topic": _persisted_value(config.mqtt.topic, default_mqtt.topic, "MQTT_TOPIC"),
                "client_id": _persisted_value(
                    config.mqtt.client_id, default_mqtt.client_id, "MQTT_CLIENT_ID"
                ),
                "discovery": _persisted_value(
                    config.mqtt.discovery, default_mqtt.discovery, "MQTT_DISCOVERY"
                ),
                "discovery_prefix": _persisted_value(
                    config.mqtt.discovery_prefix, default_mqtt.discovery_prefix, "MQTT_DISCOVERY_PREFIX"
                ),
            }
        ),
        "sources_dir": _persisted_value(config.sources_dir, Config.sources_dir, "SOURCES_DIR"),
        "source_defaults": config.source_defaults,
        "snippet_dir": _persisted_value(config.snippet_dir, Config.snippet_dir, "SNIPPET_DIR"),
        "ffmpeg_path": _persisted_value(config.ffmpeg_path, Config.ffmpeg_path, None),
        "log_level": _persisted_value(config.log_level, Config.log_level, "LOG_LEVEL"),
        "web": _drop_omitted(
            {
                "enabled": _persisted_value(config.web.enabled, default_web.enabled, "WEB_ENABLED"),
                "host": _persisted_value(config.web.host, default_web.host, "WEB_HOST"),
                "port": _persisted_value(config.web.port, default_web.port, "WEB_PORT"),
            }
        ),
        "cleanup": _drop_omitted(
            {
                "max_age_days": _persisted_value(
                    config.cleanup.max_age_days, default_cleanup.max_age_days, "CLEANUP_MAX_AGE_DAYS"
                ),
                "max_space_mb": _persisted_value(
                    config.cleanup.max_space_mb, default_cleanup.max_space_mb, "CLEANUP_MAX_SPACE_MB"
                ),
                "check_interval_minutes": _persisted_value(
                    config.cleanup.check_interval_minutes,
                    default_cleanup.check_interval_minutes,
                    "CLEANUP_CHECK_INTERVAL_MINUTES",
                ),
            }
        ),
    }
    return _drop_omitted(result)


def _dump_source(source: SourceConfig) -> dict:
    """Serialize one source back to the shape load_sources() reads."""
    return {
        "id": source.id,
        "name": source.name,
        "type": source.type,
        "path": source.path,
        "listen": source.listen,
        "thresholds": source.thresholds,
        "min_volume": source.min_volume,
        "pre_capture": source.pre_capture,
        "post_capture": source.post_capture,
        "input_args": source.input_args,
    }


def _atomic_write(path: str, prefix: str, write_fn) -> None:
    """Write to a temp file in the same directory, then rename, so a crash
    mid-write can't corrupt whatever was at `path` before."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=prefix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            write_fn(f)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise


def _atomic_write_yaml(data: dict, path: str, prefix: str) -> None:
    _atomic_write(path, prefix, lambda f: yaml.safe_dump(data, f, sort_keys=False))


def save_config(config: Config, path: str) -> None:
    """Write the app-level config back to disk atomically. Does not touch
    sources_dir - see dump_config()."""
    _atomic_write_yaml(dump_config(config), path, ".config-")


def save_source(source: SourceConfig, sources_dir: str) -> None:
    """Overwrites the existing file for source.name if one exists, otherwise
    creates a new one (see _new_source_path) - so once a source has a file,
    that file's name never changes even across a rename, only the 'name:'
    field inside it does. Callers are responsible for calling
    delete_source_file() on the OLD name first if this is a rename to a
    different name (see AppController.update_source), otherwise the old
    file would be left behind alongside the new one."""
    os.makedirs(sources_dir, exist_ok=True)
    path = _find_source_path(source.name, sources_dir) or _new_source_path(source.name, sources_dir)
    _atomic_write_yaml(_dump_source(source), path, ".source-")


def delete_source_file(name: str, sources_dir: str) -> None:
    path = _find_source_path(name, sources_dir)
    if path is not None:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
