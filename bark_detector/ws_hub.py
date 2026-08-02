"""In-process pub/sub for the websocket layer: every connected browser tab
registers an outbound queue here, and background threads (the status
watchdog, MQTT connect/disconnect callbacks) broadcast into it without
needing to know who - or how many - clients are currently listening."""

from __future__ import annotations

import queue
import threading


class WsHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._clients: set[queue.Queue] = set()

    def register(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._clients.add(q)
        return q

    def unregister(self, q: queue.Queue) -> None:
        with self._lock:
            self._clients.discard(q)

    def broadcast(self, message: dict) -> None:
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            q.put(message)
