from __future__ import annotations

import logging
import os
import signal
import sys
import threading

from .config import ConfigError, load_config
from .controller import AppController
from .web import start_web_server, stop_web_server

logger = logging.getLogger(__name__)


def main() -> int:
    # config_path can't itself live in the file it points to (chicken-and-
    # egg), so it's resolved here before load_config runs. load_config
    # resolves sources_dir from config.yaml or SOURCES_DIR.
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    if len(sys.argv) > 1:
        config_path = sys.argv[1]

    try:
        config = load_config(config_path)
    except (ConfigError, OSError) as exc:
        logging.basicConfig(level=logging.INFO)
        logger.error("failed to load config from %s: %s", config_path, exc)
        return 1

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s",
    )

    # config.yaml and sources_dir are never auto-rewritten just because the
    # app booted - they only change when the settings UI explicitly saves
    # something (see AppController), so a hand-edited file is never
    # silently touched underneath you.

    logger.info("loaded %d source(s) from %s", len(config.sources), config.sources_dir)

    shutdown_event = threading.Event()

    def handle_signal(signum, _frame) -> None:
        logger.info("received signal %s, shutting down", signum)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    controller = AppController(config, config_path, config.sources_dir)
    controller.start()

    web_server = None
    if config.web.enabled:
        web_server = start_web_server(controller, config.web.host, config.web.port)

    shutdown_event.wait()

    if web_server is not None:
        stop_web_server(web_server)
    controller.stop()
    logger.info("shutdown complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
