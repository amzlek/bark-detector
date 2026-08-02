"""Age- and space-based retention for saved snippets, plus the disk-usage
stats shown on the settings page / GET /api/stats."""

from __future__ import annotations

import glob
import logging
import os
import shutil
import time

from .config import CleanupConfig
from .event_store import EventStore
from .mqtt_publisher import MqttPublisher

logger = logging.getLogger(__name__)


def compute_stats(store: EventStore, snippet_dir: str) -> dict:
    wav_files = glob.glob(os.path.join(snippet_dir, "*.wav"))
    space_used = sum(os.path.getsize(f) for f in wav_files if os.path.isfile(f))

    try:
        space_available = shutil.disk_usage(snippet_dir).free
    except OSError:
        space_available = None

    return {
        "recordings": store.count(),
        "space_used_bytes": space_used,
        "space_available_bytes": space_available,
    }


def run_cleanup(
    store: EventStore, snippet_dir: str, config: CleanupConfig, publisher: MqttPublisher
) -> None:
    _cleanup_by_age(store, config)
    _cleanup_by_space(store, config, publisher)


def _cleanup_by_age(store: EventStore, config: CleanupConfig) -> None:
    if not config.max_age_days:
        return

    cutoff = time.time() - config.max_age_days * 86400
    victims = [
        row for row in store.list_all_ordered_by_age_asc() if row["timestamp"] < cutoff
    ]
    if not victims:
        return

    _delete_rows(store, victims)
    logger.info(
        "age-based cleanup: removed %d snippet(s) older than %.1f days",
        len(victims),
        config.max_age_days,
    )


def _cleanup_by_space(
    store: EventStore, config: CleanupConfig, publisher: MqttPublisher
) -> None:
    if not config.max_space_mb:
        return

    max_bytes = config.max_space_mb * 1_000_000
    rows = store.list_all_ordered_by_age_asc()
    sizes = {row["id"]: _safe_size(row["file"]) for row in rows}
    total = sum(sizes.values())

    if total <= max_bytes:
        return

    used_before = total
    victims = []
    for row in rows:
        if total <= max_bytes:
            break
        victims.append(row)
        total -= sizes[row["id"]]

    _delete_rows(store, victims)
    logger.warning(
        "space-based cleanup: removed %d snippet(s), %.1fMB -> under %.1fMB cap",
        len(victims),
        used_before / 1_000_000,
        config.max_space_mb,
    )

    try:
        publisher.publish_space_alert(used_before, int(max_bytes), len(victims))
    except Exception:
        logger.exception("failed to publish space-limit MQTT alert")


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _delete_rows(store: EventStore, rows: list[dict]) -> None:
    ids = []
    for row in rows:
        ids.append(row["id"])
        try:
            os.remove(row["file"])
        except OSError as exc:
            logger.warning("failed to remove snippet file %s: %s", row["file"], exc)

    store.delete_ids(ids)
