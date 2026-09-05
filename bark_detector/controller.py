"""Owns all live application state: config, running per-source workers, the
MQTT publisher, and the event store. Both main.py (at startup) and the
settings API (at runtime) go through this so the on-disk config (the app
config file plus one *.yaml per source under sources_dir), in-memory
config, and live worker threads never drift out of sync with each other."""

from __future__ import annotations

import dataclasses
import logging
import threading
import time

from .cleanup import compute_stats, run_cleanup
from .config import (
    Config,
    ConfigError,
    MqttConfig,
    SourceConfig,
    build_mqtt_config,
    build_source_config,
    delete_source_file,
    save_config,
    save_source,
)
from .event_store import EventStore
from .mqtt_publisher import MqttPublisher
from .probe import probe_source
from .worker import SourceWorker
from .ws_hub import WsHub

logger = logging.getLogger(__name__)

# "connected" means audio has actually arrived within the last STALE_SECONDS
# - not just that the ffmpeg process is alive, which can be true even when a
# stream is stuck/silent. Comfortably above the ~1s chunk interval so normal
# scheduling jitter never flaps the status.
STALE_SECONDS = 5.0
STATUS_POLL_INTERVAL = 2.0


class _ManagedWorker:
    __slots__ = ("worker", "stop_event")

    def __init__(self, worker: SourceWorker, stop_event: threading.Event):
        self.worker = worker
        self.stop_event = stop_event


class AppController:
    def __init__(self, config: Config, config_path: str, sources_dir: str):
        self.config_path = config_path
        self.sources_dir = sources_dir
        self.config = config
        self._lock = threading.RLock()
        self._workers: dict[str, _ManagedWorker] = {}
        self._source_status: dict[str, str] = {}

        self.ws_hub = WsHub()
        self.publisher = MqttPublisher(config.mqtt, on_status_change=self._on_mqtt_status_change)
        self.store = EventStore(config.db_path)

        self._background_stop_event = threading.Event()
        self._status_thread = threading.Thread(
            target=self._status_loop, name="status-watchdog", daemon=True
        )
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, name="cleanup", daemon=True
        )

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self.publisher.connect()
        with self._lock:
            for source in self.config.sources:
                self._start_worker(source)
        self._status_thread.start()
        self._cleanup_thread.start()

    def stop(self) -> None:
        self._background_stop_event.set()
        with self._lock:
            names = list(self._workers.keys())
        for name in names:
            self._stop_worker(name)
        self._status_thread.join(timeout=STATUS_POLL_INTERVAL + 2)
        self._cleanup_thread.join(timeout=2)
        self.store.close()
        self.publisher.stop()

    # -- internal worker management ------------------------------------------

    def _start_worker(self, source: SourceConfig) -> None:
        stop_event = threading.Event()
        worker = SourceWorker(self.config, source, self.publisher, self.store, stop_event)
        self._workers[source.name] = _ManagedWorker(worker, stop_event)
        worker.start()
        logger.info("started worker for source '%s'", source.name)

    def _stop_worker(self, name: str) -> None:
        with self._lock:
            managed = self._workers.pop(name, None)
            self._source_status.pop(name, None)
        if managed is None:
            return

        # order matters: set the stop flag before force-killing ffmpeg, so
        # the worker's own restart-on-failure logic sees the flag and bails
        # out instead of reconnecting right after we terminate it
        managed.stop_event.set()
        managed.worker.audio_source.stop()
        managed.worker.join(timeout=10)
        if managed.worker.is_alive():
            logger.warning("worker for source '%s' did not stop cleanly", name)
        else:
            logger.info("stopped worker for source '%s'", name)

    def _persist(self) -> None:
        save_config(self.config, self.config_path)

    # -- live connectivity status ---------------------------------------------

    def _status_loop(self) -> None:
        while not self._background_stop_event.wait(STATUS_POLL_INTERVAL):
            with self._lock:
                workers = list(self._workers.items())

            for name, managed in workers:
                last = managed.worker.last_chunk_at
                if last is None:
                    new_status = "connecting"
                elif time.time() - last < STALE_SECONDS:
                    new_status = "connected"
                else:
                    new_status = "disconnected"

                old_status = self._source_status.get(name)
                if new_status == old_status:
                    continue

                self._source_status[name] = new_status
                self.ws_hub.broadcast({"type": "source_status", "name": name, "status": new_status})
                # "connecting" only ever appears as the very first observation
                # (nothing has happened yet) - every other case is a real
                # transition worth notifying, INCLUDING the first one landing
                # on "connected" directly, which happens whenever audio
                # arrives before this loop's first tick (the common case:
                # ffmpeg connects in well under STATUS_POLL_INTERVAL)
                if new_status != "connecting":
                    try:
                        self.publisher.publish_status(name, new_status)
                    except Exception:
                        logger.exception("failed to publish status for '%s'", name)

    def all_source_status(self) -> dict[str, str]:
        with self._lock:
            return dict(self._source_status)

    def mqtt_connected(self) -> bool:
        return self.publisher.is_connected()

    def _on_mqtt_status_change(self, connected: bool) -> None:
        self.ws_hub.broadcast({"type": "mqtt_status", "connected": connected})
        # (re)publish HA discovery configs on every successful connect - the
        # only way a fresh HA instance or broker (no retained messages yet)
        # ends up with the full set of entities without a restart, and
        # cheap enough (retained, no-op if unchanged) to do unconditionally.
        # Off-thread: this callback runs ON paho's own network thread (it's
        # wired to on_connect), and each discovery publish blocks on
        # wait_for_publish() waiting for a PUBACK that thread would
        # otherwise deliver itself - calling it inline would self-block for
        # the full timeout on every topic, stalling all other MQTT traffic
        # (triggers/events/status) for as long as discovery publishing takes.
        if connected:
            with self._lock:
                sources = list(self.config.sources)
            threading.Thread(
                target=self._publish_discovery, args=(sources,), name="ha-discovery", daemon=True
            ).start()

    def _publish_discovery(self, sources: list[SourceConfig]) -> None:
        try:
            self.publisher.publish_all_discovery(sources)
        except Exception:
            logger.exception("failed to publish HA discovery configs")

    # -- cleanup ----------------------------------------------------------------

    def _cleanup_loop(self) -> None:
        while not self._background_stop_event.is_set():
            try:
                run_cleanup(self.store, self.config.snippet_dir, self.config.cleanup, self.publisher)
            except Exception:
                logger.exception("cleanup pass failed")

            if self._background_stop_event.wait(self.config.cleanup.check_interval_minutes * 60):
                break

    def get_stats(self) -> dict:
        return compute_stats(self.store, self.config.snippet_dir)

    # -- sources API (used by the settings UI) -------------------------------

    def list_sources(self) -> list[SourceConfig]:
        with self._lock:
            return list(self.config.sources)

    def add_source(self, raw: dict) -> SourceConfig:
        with self._lock:
            name = raw.get("name")
            if any(s.name == name for s in self.config.sources):
                raise ConfigError(f"source '{name}' already exists")

            source = build_source_config(raw)
            self.config.sources.append(source)
            save_source(source, self.sources_dir)
            self._start_worker(source)

        try:
            self.publisher.publish_discovery_for_source(source)
        except Exception:
            logger.exception("failed to publish HA discovery config for '%s'", source.name)
        return source

    def update_source(self, name: str, raw: dict) -> SourceConfig:
        with self._lock:
            index = next(
                (i for i, s in enumerate(self.config.sources) if s.name == name), None
            )
            if index is None:
                raise ConfigError(f"source '{name}' not found")

            merged = {**raw, "name": raw.get("name", name), "id": self.config.sources[index].id}
            new_source = build_source_config(merged)

            if new_source.name != name and any(
                s.name == new_source.name for s in self.config.sources
            ):
                raise ConfigError(f"source '{new_source.name}' already exists")

            self.config.sources[index] = new_source
            save_source(new_source, self.sources_dir)
            if new_source.name != name:
                delete_source_file(name, self.sources_dir)

        # restart outside the lock: join() can block briefly and shouldn't
        # hold up unrelated reads (e.g. list_sources) while it does
        self._stop_worker(name)
        with self._lock:
            self._start_worker(new_source)

        try:
            # same unique_id (see update_source's "id" preservation above),
            # so this overwrites the entity's existing HA config in place
            # (e.g. a new name/listen list) rather than creating a duplicate
            self.publisher.publish_discovery_for_source(new_source)
        except Exception:
            logger.exception("failed to publish HA discovery config for '%s'", new_source.name)
        return new_source

    def delete_source(self, name: str) -> None:
        with self._lock:
            removed = next((s for s in self.config.sources if s.name == name), None)
            if removed is None:
                raise ConfigError(f"source '{name}' not found")
            self.config.sources = [s for s in self.config.sources if s.name != name]
            delete_source_file(name, self.sources_dir)
        self._stop_worker(name)

        try:
            self.publisher.remove_discovery_for_source(removed.id)
        except Exception:
            logger.exception("failed to remove HA discovery config for '%s'", name)

    def test_source(self, raw: dict) -> tuple[bool, str]:
        """Probe a candidate source's connectivity without saving/starting it -
        backs the settings page's 'Test' button."""
        try:
            candidate = build_source_config({**raw, "name": raw.get("name") or "test"})
        except ConfigError as exc:
            return False, str(exc)

        return probe_source(
            candidate.type, candidate.path, candidate.input_args, self.config.ffmpeg_path
        )

    # -- mqtt API -------------------------------------------------------------

    def get_mqtt(self) -> MqttConfig:
        with self._lock:
            return self.config.mqtt

    def update_mqtt(self, raw: dict) -> MqttConfig:
        """Partial update: any field raw doesn't include keeps its current
        value, rather than resetting to MqttConfig's bare defaults - so the
        settings UI can omit 'password' entirely to mean "leave it alone"
        (see settings.html/web.py, which never send the real password back
        to the browser to begin with) without that clearing it out."""
        with self._lock:
            old_mqtt = self.config.mqtt
            merged = {**dataclasses.asdict(old_mqtt), **raw}
            new_mqtt = build_mqtt_config(merged)
            self.config.mqtt = new_mqtt
            self._persist()
            sources = list(self.config.sources)

        self.publisher.reconfigure(new_mqtt)

        # discovery just got turned off: clean up entities left over from
        # when it was on, on the (possibly new) broker - reconfigure()
        # already handles the opposite direction (turning it back on),
        # since _on_mqtt_status_change republishes everything on connect
        if old_mqtt.discovery and not new_mqtt.discovery:
            try:
                self.publisher.remove_system_discovery()
                for source in sources:
                    self.publisher.remove_discovery_for_source(source.id)
            except Exception:
                logger.exception("failed to remove HA discovery configs after disabling discovery")
        return new_mqtt
