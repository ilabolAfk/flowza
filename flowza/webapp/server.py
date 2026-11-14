"""Flowza — Flask-бэкенд: отдаёт редактор и графический API.

Flask нужен только во время запуска; при сборке в .exe PyInstaller кладёт его
внутрь, поэтому на других ПК ставить ничего не нужно.

Защита: запросы только с локального источника, лимит тела запроса, проверка
путей и URL в нодах, токен для входящих вебхуков, ограничение частоты.
"""

import json
import logging
import os
import re
import sys
import threading
import time
import uuid
import webbrowser
from collections import OrderedDict
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from .. import settings as settings_module
from ..core.engine import Engine, RunOptions, RunResult, prepare_demo
from ..core.scheduler import Scheduler
from ..model.graph import Graph
from ..model.registry import all_node_types, node_spec
from ..security import (
    RateLimiter,
    SecurityError,
    check_json,
    origin_host,
    token_ok,
)

log = logging.getLogger("flowza")

APP_VERSION = "1.1.1"
PKG_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = PKG_DIR / "web"
IS_FROZEN = getattr(sys, "frozen", False)

GRAPH_FILE = settings_module.graph_path()
SETTINGS = settings_module.SettingsStore()

if IS_FROZEN:
    bundle = Path(getattr(sys, "_MEIPASS", PKG_DIR))
    if (bundle / "web").exists():
        WEB_DIR = bundle / "web"

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
try:
    app.json.ensure_ascii = False
except AttributeError:
    app.config["JSON_AS_ASCII"] = False

_lock = threading.Lock()
_store = {"graph": None}

HISTORY_LIMIT = 50
KEEP_RESULTS = 10
MAX_WEBHOOK_BODY = 256 * 1024
ALLOWED_ORIGIN_HOSTS = {"127.0.0.1", "localhost", "::1"}
HOOK_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

RUNS = OrderedDict()
RUN_LOCK = threading.RLock()
ACTIVE = {"id": None}
HOOK_LIMIT = RateLimiter(30, 60.0)
RUN_LIMIT = RateLimiter(30, 60.0)
SCHEDULER = None


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


# ---------------------------------------------------------------- прогоны
@dataclass
class RunState:
    run_id: str
    source: str
    started_at: float
    started_wall: float = 0.0
    status: str = "running"
    finished_at: float = 0.0
    finished_wall: float = 0.0
    progress: dict = field(default_factory=dict)
    result: dict = field(default_factory=dict)
    error: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)

    def snapshot(self, with_result=False):
        elapsed = (self.finished_at or time.monotonic()) - self.started_at
        payload = {
            "run_id": self.run_id,
            "source": self.source,
            "status": self.status,
            "started_at": round(self.started_at, 3),
            "started_wall": round(self.started_wall, 3),
            "finished_wall": round(self.finished_wall, 3),
            "finished_at": round(self.finished_at, 3),
            "ms": round(elapsed * 1000, 1),
            "summary": (self.result or {}).get("summary") or {},
            "error": self.error,
            "progress": dict(self.progress),
        }
        if with_result:
            payload["result"] = self.result
        return payload


def _finish_run(state, result):
    state.finished_at = time.monotonic()
    state.finished_wall = time.time()
    state.result = result.to_dict()
    if result.cancelled:
        state.status = "cancelled"
    elif result.timed_out:
        state.status = "timeout"
    elif not result.ok:
        state.status = "error"
    else:
        state.status = "done"
    state.error = result.error or ""
    with RUN_LOCK:
        if ACTIVE["id"] == state.run_id:
            ACTIVE["id"] = None
        state.progress = {}
    log.info("Прогон %s (%s): %s", state.run_id, state.source,
             json.dumps(result.summary, ensure_ascii=False))
    _keep_results()


def _keep_results():
    with RUN_LOCK:
        states = list(RUNS.values())
    for state in states[:-KEEP_RESULTS] if len(states) > KEEP_RESULTS else []:
        if state.status != "running":
            state.result = {}


def _progress_callback(state):
    def update(payload):
        node_id = payload.get("node_id")
        if not node_id:
            return
        state.progress[node_id] = {key: value for key, value in payload.items()
                                   if key not in ("node_id", "title")}
    return update


def start_run(graph, seed, source="manual", wait=False):
    state = RunState(run_id=uuid.uuid4().hex[:12], source=source,
                     started_at=time.monotonic())
    with RUN_LOCK:
        if ACTIVE["id"]:
            return None, "уже выполняется другой прогон"
        ACTIVE["id"] = state.run_id
        RUNS[state.run_id] = state
        while len(RUNS) > HISTORY_LIMIT:
            RUNS.popitem(last=False)
    options = RunOptions.from_settings(SETTINGS.get())

    def work():
        try:
            result = Engine().run(graph, seed, options=options,
                                  progress=_progress_callback(state),
                                  cancel=state.cancel)
        except Exception as exc:
            log.exception("Прогон %s упал", state.run_id)
            failed = RunResult(started_at=state.started_at,
                               finished_at=time.monotonic(),
                               error=f"{type(exc).__name__}: {exc}")
            _finish_run(state, failed)
            return
        _finish_run(state, result)

    if wait:
        work()
        return state, None
    threading.Thread(target=work, name=f"flowza-run-{state.run_id}",
                     daemon=True).start()
    return state, None


def cancel_active():
    with RUN_LOCK:
        run_id = ACTIVE["id"]
        state = RUNS.get(run_id) if run_id else None
    if state is None:
        return None
    state.cancel.set()
    return state.run_id


def _find_run(run_id):
    with RUN_LOCK:
        return RUNS.get(run_id)


# ---------------------------------------------------------------- ошибки
@app.errorhandler(HTTPException)
def _http_error(exc):
    return jsonify({"ok": False, "error": exc.description or exc.name}), exc.code


@app.errorhandler(Exception)
def _unexpected_error(exc):
    log.exception("Необработанная ошибка")
    return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


# ---------------------------------------------------------------- защита
@app.before_request
def _guard_origin():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if not request.path.startswith("/api/"):
        return None
    origin = request.headers.get("Origin")
    if not origin:
        return None
    if origin_host(origin) not in ALLOWED_ORIGIN_HOSTS:
        return jsonify({"ok": False, "error": "запрос с другого источника отклонён"}), 403
    return None


@app.after_request
def _security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'")
    return response


# ---------------------------------------------------------------- страница
@app.route("/")
def index():
    return send_from_directory(str(WEB_DIR), "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(str(WEB_DIR), filename)


@app.route("/api/health")
def api_health():
    return jsonify({
        "ok": True,
        "version": APP_VERSION,
        "graph_file": str(GRAPH_FILE),
        "settings_file": str(settings_module.settings_path()),
        "workspace": str(settings_module.workspace(SETTINGS.get())),
        "active_run": ACTIVE["id"],
        "scheduler": {"runs": SCHEDULER.runs if SCHEDULER else 0,
                      "skipped": SCHEDULER.skipped if SCHEDULER else 0},
    })


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


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return jsonify({"ok": True, "settings": settings_module.public(SETTINGS.get()),
                        "warnings": SETTINGS.warnings})
    data = _json_body()
    try:
        saved, warnings = SETTINGS.update(data)
    except OSError as exc:
        return jsonify({"ok": False,
                        "error": f"не удалось сохранить настройки: {exc}"}), 500
    return jsonify({"ok": True, "settings": settings_module.public(saved),
                    "warnings": warnings})


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
    SETTINGS.reload()
    return jsonify(graph_to_payload(_current_graph()))


@app.route("/api/validate", methods=["POST"])
def api_validate():
    graph = build_graph_from_payload(_json_body())
    return jsonify({"ok": True, "issues": graph.validate(),
                    "warnings": graph.load_warnings})


@app.route("/api/run", methods=["POST"])
def api_run():
    if not RUN_LIMIT.allow(request.remote_addr or "local"):
        return jsonify({"ok": False, "error": "слишком много запусков: подождите"}), 429
    payload = _json_body()
    graph = build_graph_from_payload(payload)
    seed = payload.get("seed") or {}
    if not isinstance(seed, dict) or len(seed) > 20:
        abort(400, description="seed должен быть небольшим объектом")
    wait = request.args.get("wait") in ("1", "true", "yes")
    state, error = start_run(graph, seed, "manual", wait=wait)
    if error:
        return jsonify({"ok": False, "error": error}), 409
    if wait:
        body = dict(state.result or {})
        body.update({"ok": state.status == "done", "run_id": state.run_id,
                     "status": state.status, "warnings": graph.load_warnings})
        return jsonify(body)
    return jsonify({"ok": True, "run_id": state.run_id, "status": state.status,
                    "run_url": f"/api/run/{state.run_id}"}), 202


@app.route("/api/run/<run_id>")
def api_run_state(run_id):
    state = _find_run(run_id)
    if state is None:
        abort(404, description="прогон не найден")
    with RUN_LOCK:
        finished = state.status != "running"
    body = state.snapshot(with_result=finished)
    body.update({"ok": True})
    return jsonify(body)


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    run_id = cancel_active()
    if not run_id:
        return jsonify({"ok": False, "error": "нет активного прогона"}), 409
    return jsonify({"ok": True, "run_id": run_id})


@app.route("/api/runs")
def api_runs():
    with RUN_LOCK:
        states = list(RUNS.values())
    states.reverse()
    return jsonify({"ok": True,
                    "runs": [state.snapshot(with_result=False)
                             for state in states[:HISTORY_LIMIT]]})


@app.route("/api/save", methods=["POST"])
def api_save():
    graph = build_graph_from_payload(_json_body())
    cfg = SETTINGS.get()
    try:
        GRAPH_FILE.parent.mkdir(parents=True, exist_ok=True)
        _rotate_backups(GRAPH_FILE, int(cfg["files"]["backups"]))
        tmp = GRAPH_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(graph.to_dict(), fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, GRAPH_FILE)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"не удалось сохранить: {exc}"}), 500
    with _lock:
        _store["graph"] = graph
    return jsonify({"ok": True, "file": str(GRAPH_FILE),
                    "nodes": len(graph.nodes), "edges": len(graph.edges)})


def _rotate_backups(path, keep):
    if keep <= 0 or not path.exists():
        return
    for index in range(keep, 0, -1):
        source = path if index == 1 else path.with_name(f"{path.name}.bak{index - 1}")
        target = path.with_name(f"{path.name}.bak{index}")
        if source.exists():
            os.replace(source, target)


# ---------------------------------------------------------------- вебхук
def _hook_node(graph, key):
    for node in graph.nodes.values():
        if node.node_type != "trigger.webhook":
            continue
        raw = str(node.params.get("path") or "").strip().strip("/")
        if raw.startswith("hook/"):
            raw = raw[len("hook/"):]
        if not raw:
            raw = node.node_id
        if raw == key:
            return node
    return None


@app.route("/hook/<key>", methods=["POST"])
def hook(key):
    cfg = SETTINGS.get()
    if not cfg["webhook"]["enabled"]:
        abort(404)
    if not HOOK_KEY.match(key):
        abort(404)
    client = request.remote_addr or "?"
    if not HOOK_LIMIT.allow(client):
        return jsonify({"ok": False, "error": "слишком много запросов"}), 429

    token_env = cfg["webhook"]["token_env"]
    expected = os.environ.get(token_env, "")
    if expected:
        provided = (request.headers.get("X-Flowza-Token")
                    or request.args.get("token") or "")
        if not token_ok(provided, expected):
            return jsonify({"ok": False, "error": "неверный токен"}), 401
    elif client not in ALLOWED_ORIGIN_HOSTS:
        return jsonify({"ok": False,
                        "error": "токен не задан: внешние запросы запрещены"}), 403

    if request.content_length and request.content_length > MAX_WEBHOOK_BODY:
        return jsonify({"ok": False, "error": "тело запроса больше 256 КБ"}), 413
    body = request.get_json(silent=True)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "ожидался JSON-объект"}), 400
    try:
        check_json(body)
    except SecurityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 413

    graph = _current_graph()
    node = _hook_node(graph, key)
    if node is None:
        abort(404)
    seed = {node.node_id: {"event": "webhook", "key": key, "body": body}}
    state, error = start_run(graph, seed, source="webhook")
    if error:
        return jsonify({"ok": False, "error": error}), 409
    return jsonify({"ok": True, "run_id": state.run_id,
                    "status": state.status}), 202


# ---------------------------------------------------------------- запуск
def _setup_logging():
    logger = logging.getLogger("flowza")
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    try:
        GRAPH_FILE.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(settings_module.runs_log_path(),
                                      maxBytes=512_000, backupCount=2,
                                      encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    except OSError:
        pass


def _scheduler_runner(node_id, interval):
    graph = _current_graph()
    if node_id not in graph.nodes:
        return False
    seed = {node_id: {"event": "schedule", "interval": interval}}
    _state, error = start_run(graph, seed, source="schedule")
    if error:
        log.info("Расписание %s пропущено: %s", node_id, error)
        return False
    return True


def launch(host="127.0.0.1", port=8765, open_browser=True):
    global SCHEDULER
    _setup_logging()
    SCHEDULER = Scheduler(graph_provider=_current_graph,
                          runner=_scheduler_runner, tick=1.0)
    SCHEDULER.start()
    url = f"http://{host}:{port}"
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"\n[Flowza] Редактор запущен: {url}")
    print(f"[Flowza] Граф сохраняется в: {GRAPH_FILE}")
    print(f"[Flowza] Настройки: {settings_module.settings_path()}")
    print(f"[Flowza] Рабочая папка для файлов: {settings_module.workspace(SETTINGS.get())}")
    print("[Flowza] Для остановки: Ctrl+C\n", flush=True)
    try:
        app.run(host=host, port=port, debug=False, threaded=True)
    finally:
        SCHEDULER.stop()


if __name__ == "__main__":
    launch(open_browser=False)
