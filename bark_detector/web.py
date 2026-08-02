"""Web UI: event log + a settings page for managing sources and the MQTT
broker at runtime. Built on Flask (not FastAPI) to keep the dependency
footprint small (no starlette/pydantic/uvicorn) while still making it easy
to add more endpoints later.

Routes:
  GET  /                      -> static/index.html (event log)
  GET  /settings              -> static/settings.html
  GET  /api/events            -> JSON list of recent detections (?limit=&before=)
  GET  /snippets/<filename>   -> the saved WAV file for a detection
  GET  /api/sources           -> list configured sources (with live status)
  POST /api/sources           -> add a source
  PUT  /api/sources/<name>    -> update a source
  DELETE /api/sources/<name>  -> remove a source
  POST /api/sources/test      -> probe connectivity for a not-yet-saved source
  GET  /api/mqtt              -> current MQTT broker settings
  PUT  /api/mqtt              -> update MQTT broker settings
  GET  /api/stats             -> recordings count + disk usage
"""

from __future__ import annotations

import dataclasses
import logging
import os
import threading

from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from werkzeug.serving import BaseWSGIServer, make_server

from .config import ConfigError
from .controller import AppController

logger = logging.getLogger(__name__)

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def create_app(controller: AppController) -> Flask:
    app = Flask(__name__, static_folder=None)

    @app.errorhandler(ConfigError)
    def handle_config_error(exc: ConfigError):
        return jsonify({"error": str(exc)}), 400

    @app.get("/")
    def index():
        return send_from_directory(_STATIC_DIR, "index.html")

    @app.get("/settings")
    def settings_page():
        return send_from_directory(_STATIC_DIR, "settings.html")

    # -- events / snippets --------------------------------------------------

    @app.get("/api/events")
    def list_events():
        limit = min(request.args.get("limit", 50, type=int) or 50, 200)
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

    # -- sources --------------------------------------------------------------

    @app.get("/api/sources")
    def list_sources():
        statuses = controller.all_source_status()
        result = []
        for source in controller.list_sources():
            entry = dataclasses.asdict(source)
            entry["status"] = statuses.get(source.name, "connecting")
            result.append(entry)
        return jsonify(result)

    @app.post("/api/sources")
    def add_source():
        source = controller.add_source(request.get_json(force=True) or {})
        return jsonify(dataclasses.asdict(source)), 201

    @app.put("/api/sources/<name>")
    def update_source(name: str):
        source = controller.update_source(name, request.get_json(force=True) or {})
        return jsonify(dataclasses.asdict(source))

    @app.delete("/api/sources/<name>")
    def delete_source(name: str):
        controller.delete_source(name)
        return "", 204

    @app.post("/api/sources/test")
    def test_source():
        ok, message = controller.test_source(request.get_json(force=True) or {})
        return jsonify({"ok": ok, "message": message}), (200 if ok else 502)

    # -- mqtt -------------------------------------------------------------------

    @app.get("/api/mqtt")
    def get_mqtt():
        return jsonify(dataclasses.asdict(controller.get_mqtt()))

    @app.put("/api/mqtt")
    def update_mqtt():
        mqtt_config = controller.update_mqtt(request.get_json(force=True) or {})
        return jsonify(dataclasses.asdict(mqtt_config))

    # -- stats ------------------------------------------------------------------

    @app.get("/api/stats")
    def get_stats():
        return jsonify(controller.get_stats())

    return app


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
