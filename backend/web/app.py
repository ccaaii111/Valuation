"""Flask shell around backend.web.api: the JSON routes plus the four pages (home, search, panel, derivation).

Binds to 127.0.0.1 by default — this is a single-user local tool; the only network egress is a 取数 job
that talks to the free public data sources. Pages are static files under static/, served by path so the
URLs stay readable; /api/* keeps its shape independent of them.
"""
from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from backend.web import api

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _failure(exc: Exception) -> tuple:
    status = 404 if isinstance(exc, FileNotFoundError) else 400
    return jsonify({"error": str(exc)}), status


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    app.json.ensure_ascii = False

    @app.get("/")
    def home():
        return send_from_directory(STATIC_DIR, "home.html")

    @app.get("/search")
    def search():
        return send_from_directory(STATIC_DIR, "search.html")

    @app.get("/panel")
    def panel_page():
        return send_from_directory(STATIC_DIR, "panel.html")

    @app.get("/derivation")
    def derivation_page():
        return send_from_directory(STATIC_DIR, "derivation.html")

    @app.get("/assets/<path:name>")
    def assets(name: str):
        return send_from_directory(STATIC_DIR, name)

    @app.get("/api/options")
    def options():
        return jsonify(api.options())

    @app.get("/api/universe")
    def universe():
        try:
            return jsonify(api.universe_payload())
        except (ValueError, FileNotFoundError) as exc:
            return _failure(exc)

    @app.get("/api/panel")
    def panel():
        try:
            mid_year = request.args.get("mid_year")
            return jsonify(
                api.panel_payload(
                    request.args.get("code", ""),
                    request.args.get("run", ""),
                    method=request.args.get("method", "fcff"),
                    terminal=request.args.get("terminal", "gordon"),
                    rate=request.args.get("rate") or None,
                    mid_year=None if mid_year in (None, "") else mid_year == "1",
                    check_consistency=request.args.get("light") != "1",
                )
            )
        except (ValueError, FileNotFoundError) as exc:  # noqa: PERF203 - one boundary, both meanings
            return _failure(exc)

    @app.get("/api/derivation")
    def derivation():
        # 骨架只读 baseline.json：同一份基线永远画同样的线，所以改了数只要重算值（/api/recompute），这里不必再来一次。
        try:
            return jsonify(api.derivation_payload(request.args.get("code", ""), request.args.get("run", "")))
        except (ValueError, FileNotFoundError) as exc:
            return _failure(exc)

    @app.post("/api/recompute")
    def recompute():
        body = request.get_json(silent=True) or {}
        try:
            mid_year = body.get("mid_year")
            return jsonify(
                api.panel_payload(
                    str(body.get("code", "")),
                    str(body.get("run", "")),
                    overrides=body.get("overrides") or {},
                    method=body.get("method", "fcff"),
                    terminal=body.get("terminal", "gordon"),
                    rate=body.get("rate"),
                    mid_year=None if mid_year is None else bool(mid_year),
                    check_consistency=not body.get("light"),
                )
            )
        except (ValueError, FileNotFoundError) as exc:
            return _failure(exc)

    @app.post("/api/save")
    def save():
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(
                api.save_manual(str(body.get("code", "")), str(body.get("run", "")), body)
            )
        except (ValueError, FileNotFoundError) as exc:
            return _failure(exc)

    @app.get("/api/valuation_status")
    def valuation_status():
        # The button's own 「要不要联网」 answer — reads runs/ 与股票名录, never the network.
        try:
            return jsonify(
                api.valuation_status(
                    request.args.get("code", ""),
                    request.args.get("run", ""),
                    beta_given=bool(request.args.get("beta", "").strip()),
                    benchmark=request.args.get("benchmark", ""),
                )
            )
        except (ValueError, FileNotFoundError) as exc:
            return _failure(exc)

    @app.post("/api/fetch")
    def fetch():
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(api.start_fetch(body))
        except (ValueError, FileNotFoundError) as exc:
            return _failure(exc)

    @app.get("/api/job")
    def job():
        try:
            return jsonify(api.job_status(request.args.get("job", "")))
        except (ValueError, FileNotFoundError) as exc:
            return _failure(exc)

    return app
