"""Flowza — Flask-бэкенд: отдаёт редактор и графический API.

Flask нужен только во время запуска; при сборке в .exe PyInstaller кладёт его
внутрь, поэтому на других ПК ставить ничего не нужно.

Пути переносимы: считаются относительно расположения файлов приложения
(Path(__file__)), а при сборке в exe — из системной папки _MEIPASS.
"""

import json
import logging
import os
import sys
import threading
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from ..core.engine import Engine, prepare_demo
from ..model.graph import Graph
from ..model.registry import all_node_types, node_spec

log = logging.getLogger("flowza")

PKG_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = PKG_DIR / "web"
IS_FROZEN = getattr(sys, "frozen", False)


def _default_graph_path():
    override = os.environ.get("FLOWZA_GRAPH")
    if override:
        return Path(override).expanduser().resolve()
    if IS_FROZEN:
        return Path.home() / "Documents" / "Flowza" / "flowza_graph.json"
    return PKG_DIR.parent / "flowza_graph.json"


GRAPH_FILE = _default_graph_path()

# При сборке PyInstaller (--onefile) статика распаковывается в _MEIPASS.
if IS_FROZEN:
    bundle = Path(getattr(sys, "_MEIPASS", PKG_DIR))
    if (bundle / "web").exists():
        WEB_DIR = bundle / "web"

app = Flask(__name__, static_folder=None)
try:  # Flask 3: не экранировать кириллицу в JSON
    app.json.ensure_ascii = False
except AttributeError:
    app.config["JSON_AS_ASCII"] = False

_lock = threading.Lock()
_store = {"graph": None}


# ---------------------------------------------------------------- сериализация
def graph_to_payload(graph):
    nodes = []
    for n in graph.nodes.values():
        spec = node_spec(n.node_type) or {}
        nodes.append({
            "id": n.node_id,
            "type": n.node_type,
            "title": n.title,
            "x": n.pos[0],
            "y": n.pos[1],
            "params": n.params,
            "inputs": [{"name": p.name, "label": p.label, "kind": p.kind}
                       for p in n.inputs],
            "outputs": [{"name": p.name, "label": p.label, "kind": p.kind}
                        for p in n.outputs],
            "spec": {
                "icon": spec.get("icon"),
                "color": spec.get("color"),
                "category": spec.get("category"),
                "params": spec.get("params", []),
                "note": spec.get("note"),
            },
        })
    edges = [{"id": e.edge_id, "source": e.source_id,
              "source_port": e.source_port,
              "target": e.target_id, "target_port": e.target_port}
             for e in graph.edges]
    return {"nodes": nodes, "edges": edges,
            "warnings": list(graph.load_warnings)}


def build_graph_from_payload(payload):
    return Graph.from_dict(payload)


def _json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, description="ожидался JSON-объект в теле запроса")
    return data


def _current_graph():
    with _lock:
        graph = _store.get("graph")
        if graph is None:
            graph = _load_graph()
            _store["graph"] = graph
    return graph


def _load_graph():
    if GRAPH_FILE.exists():
        try:
            with open(GRAPH_FILE, encoding="utf-8") as fh:
                return Graph.from_dict(json.load(fh))
        except (OSError, ValueError) as exc:
            log.warning("Не удалось прочитать %s: %s", GRAPH_FILE, exc)
    return prepare_demo()


# ---------------------------------------------------------------- ошибки
@app.errorhandler(HTTPException)
def _http_error(exc):
    return jsonify({"ok": False, "error": exc.description or exc.name}), exc.code


@app.errorhandler(Exception)
def _unexpected_error(exc):
    log.exception("Необработанная ошибка")
    return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


# ---------------------------------------------------------------- страница
@app.route("/")
def index():
    return send_from_directory(str(WEB_DIR), "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(str(WEB_DIR), filename)


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "graph_file": str(GRAPH_FILE)})


# ---------------------------------------------------------------- API
@app.route("/api/specs")
def api_specs():
    out = {}
    for node_type, spec in all_node_types().items():
        out[node_type] = {
            "title": spec["title"],
            "icon": spec.get("icon"),
            "color": spec.get("color"),
            "category": spec.get("category"),
            "note": spec.get("note"),
            "inputs": [{"name": p["name"], "label": p.get("label", p["name"]),
                        "kind": p.get("kind", "data")} for p in spec.get("inputs", [])],
            "outputs": [{"name": p["name"], "label": p.get("label", p["name"]),
                         "kind": p.get("kind", "data")} for p in spec.get("outputs", [])],
            "params": spec.get("params", []),
        }
    return jsonify(out)


@app.route("/api/graph", methods=["GET", "POST"])
def api_graph():
    if request.method == "GET":
        return jsonify(graph_to_payload(_current_graph()))
    graph = build_graph_from_payload(_json_body())
    with _lock:
        _store["graph"] = graph
    return jsonify({"ok": True, "nodes": len(graph.nodes), "edges": len(graph.edges),
                    "warnings": graph.load_warnings})


@app.route("/api/reload", methods=["POST"])
def api_reload():
    with _lock:
        _store["graph"] = None
    return jsonify(graph_to_payload(_current_graph()))


@app.route("/api/validate", methods=["POST"])
def api_validate():
    graph = build_graph_from_payload(_json_body())
    return jsonify({"ok": True, "issues": graph.validate(),
                    "warnings": graph.load_warnings})


@app.route("/api/run", methods=["POST"])
def api_run():
    payload = _json_body()
    graph = build_graph_from_payload(payload)
    seed = payload.get("seed") or {}
    if not isinstance(seed, dict):
        abort(400, description="seed должен быть объектом")
    outcome = Engine().run(graph, seed)
    body = outcome.to_dict()
    body["warnings"] = graph.load_warnings
    return jsonify(body)


@app.route("/api/save", methods=["POST"])
def api_save():
    graph = build_graph_from_payload(_json_body())
    try:
        GRAPH_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = GRAPH_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(graph.to_dict(), fh, ensure_ascii=False, indent=2)
        os.replace(tmp, GRAPH_FILE)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"не удалось сохранить: {exc}"}), 500
    with _lock:
        _store["graph"] = graph
    return jsonify({"ok": True, "file": str(GRAPH_FILE),
                    "nodes": len(graph.nodes), "edges": len(graph.edges)})


# ---------------------------------------------------------------- запуск
def _setup_logging():
    logger = logging.getLogger("flowza")
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    try:
        GRAPH_FILE.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(GRAPH_FILE.with_name("flowza_runs.log"),
                                      maxBytes=512_000, backupCount=2,
                                      encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    except OSError:
        pass


def launch(host="127.0.0.1", port=8765, open_browser=True):
    _setup_logging()
    url = f"http://{host}:{port}"
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"\n[Flowza] Редактор запущен: {url}")
    print(f"[Flowza] Граф сохраняется в: {GRAPH_FILE}")
    print(f"[Flowza] Для остановки: Ctrl+C\n", flush=True)
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    launch(open_browser=False)
