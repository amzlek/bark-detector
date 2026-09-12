"""Web UI: event log + a settings page for managing sources and the MQTT
broker at runtime. Built on Flask (not FastAPI) to keep the dependency
footprint small (no starlette/pydantic/uvicorn) while still making it easy
to add more endpoints later. Flask-Sock adds the websocket route on top of
the same threaded werkzeug dev server - no separate ASGI stack needed.

Routes:
  GET  /                      -> templates/index.html (event log)
  GET  /settings              -> templates/settings.html
  GET  /ws-client.js          -> shared websocket client used by both pages
  GET  /style.css             -> shared stylesheet used by both pages
  GET  /api/events            -> JSON list of recent detections (?limit=&before=)
  GET  /snippets/<filename>   -> the saved WAV file for a detection
  GET  /api/sources           -> list configured sources (with live status)
  GET  /api/stats             -> recordings count + disk usage
  WS   /ws                    -> everything else: settings CRUD (add/edit/
                                  delete source, update MQTT) as request/
                                  response messages, plus server-pushed
                                  source/MQTT status.

Settings CRUD used to be POST/PUT/DELETE on /api/sources and /api/mqtt;
those routes are gone now that the websocket is the only way to mutate
config (see notes.tmp for the rationale) - GET /api/sources stays since
read-only endpoints remain public HTTP.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import queue
import threading
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, render_template, request, send_file, send_from_directory
from flask_sock import Sock
from werkzeug.serving import BaseWSGIServer, make_server

from .config import ConfigError
from .controller import AppController

logger = logging.getLogger(__name__)

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# how often the ws handler wakes up (even with nothing received) to drain
# this client's outbound queue and push any pending broadcast
_WS_POLL_INTERVAL = 1.0


def create_app(controller: AppController) -> Flask:
    app = Flask(__name__, static_folder=None)
    sock = Sock(app)

    @app.errorhandler(ConfigError)
    def handle_config_error(exc: ConfigError):
        return jsonify({"error": str(exc)}), 400

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/settings")
    def settings_page():
        return render_template("settings.html")

    @app.get("/ws-client.js")
    def ws_client_js():
        return send_from_directory(_STATIC_DIR, "ws-client.js")

    @app.get("/style.css")
    def style_css():
        return send_from_directory(_STATIC_DIR, "style.css")

    # -- events / snippets --------------------------------------------------

    @app.get("/api/events")
    def list_events():
        limit = max(1, min(request.args.get("limit", 50, type=int) or 50, 200))
        before = request.args.get("before", type=float)
        events = controller.store.list_recent(limit=limit, before=before)
        for event in events:
            event["snippet_url"] = f"/snippets/{os.path.basename(event['file'])}"
        return jsonify(events)

    @app.get("/snippets/<path:filename>")
    def get_snippet(filename: str):
        # basename strips any path components, so this can't escape snippet_dir
        safe_name = os.path.basename(filename)
        if not safe_name or safe_name != filename:
            abort(400)

        full_path = os.path.join(controller.config.snippet_dir, safe_name)
        if not os.path.isfile(full_path):
            abort(404)

        return send_file(full_path, mimetype="audio/wav")

    # -- sources (read-only; CRUD moved to the websocket) ----------------------

    @app.get("/api/sources")
    def list_sources():
        statuses = controller.all_source_status()
        result = []
        for source in controller.list_sources():
            entry = dataclasses.asdict(source)
            entry["status"] = statuses.get(source.name, "connecting")
            result.append(entry)
        return jsonify(result)

    # -- stats ------------------------------------------------------------------

    @app.get("/api/stats")
    def get_stats():
        return jsonify(controller.get_stats())

    # -- websocket: live status + settings CRUD ---------------------------------

    @sock.route("/ws")
    def ws_endpoint(ws):
        if not _valid_ws_origin(request.headers.get("Origin"), request.host):
            ws.close(reason="invalid origin")
            return
        _serve_ws_client(controller, ws)

    return app


def _valid_ws_origin(origin: str | None, host: str) -> bool:
    # Browsers always include Origin on a WebSocket handshake. Non-browser
    # LAN clients may omit it; cross-site browser scripts cannot.
    if origin is None:
        return True
    try:
        parsed = urlsplit(origin)
        return parsed.scheme in ("http", "https") and parsed.netloc == host and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
    except ValueError:
        return False


def _serve_ws_client(controller: AppController, ws) -> None:
    outbound: queue.Queue = controller.ws_hub.register()
    try:
        _send(
            ws,
            {
                "type": "hello",
                "source_status": controller.all_source_status(),
                "mqtt_connected": controller.mqtt_connected(),
            },
        )

        while True:
            try:
                raw = ws.receive(timeout=_WS_POLL_INTERVAL)
            except Exception:
                break
            if raw is None and getattr(ws, "connected", True) is False:
                break
            if raw is not None:
                _dispatch(controller, ws, raw)

            try:
                _drain_outbound(ws, outbound)
            except Exception:
                break
    finally:
        controller.ws_hub.unregister(outbound)


def _drain_outbound(ws, outbound: queue.Queue) -> None:
    while True:
        try:
            message = outbound.get_nowait()
        except queue.Empty:
            return
        _send(ws, message)


def _dispatch(controller: AppController, ws, raw) -> None:
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        _send(ws, {"type": "error", "error": "invalid JSON"})
        return

    action = msg.get("type")
    req_id = msg.get("id")
    payload = msg.get("payload") or {}

    try:
        if action == "sources.add":
            source = controller.add_source(payload)
            _reply(ws, action, req_id, dataclasses.asdict(source))
        elif action == "sources.update":
            source = controller.update_source(msg.get("name"), payload)
            _reply(ws, action, req_id, dataclasses.asdict(source))
        elif action == "sources.delete":
            controller.delete_source(msg.get("name"))
            _reply(ws, action, req_id, {"name": msg.get("name")})
        elif action == "sources.test":
            ok, message = controller.test_source(payload)
            _reply(ws, action, req_id, {"ok": ok, "message": message})
        elif action == "mqtt.get":
            _reply(ws, action, req_id, _redact_mqtt(controller.get_mqtt()))
        elif action == "mqtt.update":
            mqtt_config = controller.update_mqtt(payload)
            _reply(ws, action, req_id, _redact_mqtt(mqtt_config))
        else:
            _fail(ws, action, req_id, f"unknown message type '{action}'")
    except ConfigError as exc:
        _fail(ws, action, req_id, str(exc))
    except Exception:
        logger.exception("unhandled error processing ws message '%s'", action)
        _fail(ws, action, req_id, "internal error")


def _redact_mqtt(mqtt_config) -> dict:
    """The real password never goes to the browser, not even once - the
    settings page only needs to know whether one is already set (to show
    a hint) and never needs to display or resubmit the actual value (see
    templates/settings.html, which treats the password field as write-
    only: blank means "leave it alone", handled by
    AppController.update_mqtt's merge-not-replace semantics)."""
    data = dataclasses.asdict(mqtt_config)
    data["has_password"] = bool(data.pop("password"))
    return data


def _reply(ws, action, req_id, data) -> None:
    _send(ws, {"type": action, "id": req_id, "ok": True, "data": data})


def _fail(ws, action, req_id, error: str) -> None:
    _send(ws, {"type": action, "id": req_id, "ok": False, "error": error})


def _send(ws, message: dict) -> None:
    ws.send(json.dumps(message))


def start_web_server(controller: AppController, host: str, port: int) -> BaseWSGIServer:
    """Caller owns shutdown: call stop_web_server(server) when done, from the
    same thread that decided to stop (see main.py) rather than racing a
    second watcher thread against it."""
    app = create_app(controller)
    server = make_server(host, port, app, threaded=True)

    thread = threading.Thread(target=server.serve_forever, name="web", daemon=True)
    thread.start()
    logger.info("web UI listening on http://%s:%d", host, port)

    return server


def stop_web_server(server: BaseWSGIServer) -> None:
    # shutdown() must complete (serve_forever's loop must actually exit)
    # before server_close() touches the socket, or the two can race
    server.shutdown()
    server.server_close()
