"""Движок выполнения графа: интерпретирует модель (ноды + рёбра).

Не зависит от UI. Обходит все ноды в топологическом порядке и возвращает
результат по каждой ноде плюс отчёт: выполнено / пропущено / ошибка / цикл.
Поддерживает таймаут, повторы, паузу между нодами, политику ошибок и отмену.
"""

import logging
import time
from dataclasses import dataclass, field, fields

from ..model.graph import Graph, find_cycles, topological_order
from ..model.registry import NodeError, run_node
from .context import CANCELLED, TIMEOUT, RunAborted, RunContext

log = logging.getLogger("flowza")

DONE = "done"
SKIPPED = "skipped"
ERROR = "error"
CYCLE = "cycle"


@dataclass
class RunOptions:
    """Параметры прогона из настроек."""

    pause_ms: int = 0
    timeout_s: int = 120
    on_error: str = "continue"
    retries: int = 0

    @classmethod
    def from_settings(cls, settings):
        run = (settings or {}).get("run", {})
        return cls(
            pause_ms=int(run.get("pause_ms") or 0),
            timeout_s=int(run.get("timeout_s") or 120),
            on_error=str(run.get("on_error") or "continue"),
            retries=int(run.get("retries") or 0),
        )

    def to_dict(self):
        return {item.name: getattr(self, item.name) for item in fields(self)}


@dataclass
class RunResult:
    """Результат прогона: значения по нодам + отчёт о статусах."""

    results: dict = field(default_factory=dict)
    report: dict = field(default_factory=dict)
    order: list = field(default_factory=list)
    cancelled: bool = False
    timed_out: bool = False
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def ok(self):
        return (not self.cancelled and not self.timed_out
                and not any(item["status"] == ERROR for item in self.report.values()))

    @property
    def summary(self):
        counts = {DONE: 0, SKIPPED: 0, ERROR: 0, CYCLE: 0}
        for item in self.report.values():
            counts[item["status"]] += 1
        return counts

    @property
    def ms(self):
        if not self.finished_at:
            return 0
        return round((self.finished_at - self.started_at) * 1000, 1)

    def to_dict(self):
        return {"ok": self.ok, "result": self.results, "report": self.report,
                "order": self.order, "summary": self.summary,
                "cancelled": self.cancelled, "timed_out": self.timed_out,
                "error": self.error, "ms": self.ms}


def _emit(progress, payload):
    if progress is None:
        return
    try:
        progress(payload)
    except Exception:
        log.debug("обработчик прогресса упал", exc_info=True)


class Engine:
    """Топологический проход по всему графу с отчётом по каждой ноде."""

    def run(self, graph, seed=None, options=None, progress=None, cancel=None):
        """seed: {node_id или node_type: стартовое значение}."""
        options = options or RunOptions()
        seed = seed or {}
        result = RunResult(started_at=time.monotonic())
        deadline = result.started_at + options.timeout_s if options.timeout_s else None
        context = RunContext(cancel=cancel, deadline=deadline)
        order, cycles = topological_order(graph)
        result.order = order
        total = len(order)
        stopped = None

        for index, node_id in enumerate(order):
            node = graph.nodes[node_id]
            if stopped is not None:
                entry = {"status": SKIPPED, "ms": 0, "reason": stopped}
                result.report[node_id] = entry
                _emit(progress, {"node_id": node_id, "title": node.title,
                                 "index": index, "total": total, **entry})
                continue

            try:
                context.check()
            except RunAborted as exc:
                self._stop(result, exc)
                stopped = exc.message
                entry = {"status": SKIPPED, "ms": 0, "reason": exc.message}
                result.report[node_id] = entry
                _emit(progress, {"node_id": node_id, "title": node.title,
                                 "index": index, "total": total, **entry})
                continue

            _emit(progress, {"node_id": node_id, "title": node.title,
                             "index": index, "total": total, "status": "running"})
            entry = self._run_node(graph, node, seed, result, options, context)
            result.report[node_id] = entry

            if entry["status"] == ERROR and options.on_error == "stop":
                stopped = f"прогон остановлен после ошибки «{node.title}»"
            if stopped is None and entry["status"] == DONE and options.pause_ms:
                try:
                    context.sleep(options.pause_ms / 1000.0)
                except RunAborted as exc:
                    self._stop(result, exc)
                    stopped = exc.message
            _emit(progress, {"node_id": node_id, "title": node.title,
                             "index": index, "total": total, **entry})

        for node_id in cycles:
            node = graph.nodes[node_id]
            entry = {"status": CYCLE, "ms": 0, "reason": "нода участвует в цикле"}
            result.report[node_id] = entry
            _emit(progress, {"node_id": node_id, "title": node.title,
                             "index": total, "total": total, **entry})

        result.finished_at = time.monotonic()
        if stopped and not result.error:
            result.error = stopped
        return result

    @staticmethod
    def _stop(result, exc):
        if exc.kind == CANCELLED:
            result.cancelled = True
        elif exc.kind == TIMEOUT:
            result.timed_out = True
        result.error = exc.message

    def _inputs_for(self, graph, node_id, result):
        node = graph.nodes[node_id]
        edge_list = graph.incoming(node_id)
        inputs = {}
        broken_parent = None
        for edge in edge_list:
            parent_status = result.report.get(edge.source_id, {}).get("status")
            if parent_status in (ERROR, SKIPPED, CYCLE):
                broken_parent = graph.nodes[edge.source_id].title
                break
            parent_outputs = result.results.get(edge.source_id) or {}
            inputs[edge.target_port] = parent_outputs.get(edge.source_port)
        return node, edge_list, inputs, broken_parent

    def _run_node(self, graph, node, seed, result, options, context):
        node_id = node.node_id
        started = time.perf_counter()
        node, edge_list, inputs, broken_parent = self._inputs_for(graph, node_id, result)

        if broken_parent:
            return {"status": SKIPPED, "ms": 0,
                    "reason": f"ветка не выполнилась: «{broken_parent}»"}
        if node.inputs and not edge_list:
            return {"status": SKIPPED, "ms": 0, "reason": "вход ни к чему не подключён"}
        if edge_list and all(value is None for value in inputs.values()):
            return {"status": SKIPPED, "ms": 0, "reason": "нет данных: ветка не активна"}
        if not edge_list:
            value = seed.get(node_id, seed.get(node.node_type))
            inputs["_seed"] = value if value is not None else {"event": node.node_type}

        attempts = 0
        while True:
            attempts += 1
            try:
                outputs = run_node(node, inputs, context)
            except RunAborted as exc:
                self._stop(result, exc)
                return {"status": SKIPPED, "ms": self._ms(started),
                        "reason": exc.message, "attempts": attempts}
            except NodeError as exc:
                if attempts <= options.retries and not context.expired():
                    try:
                        context.sleep(0.2)
                    except RunAborted as aborted:
                        self._stop(result, aborted)
                        return {"status": SKIPPED, "ms": self._ms(started),
                                "reason": aborted.message, "attempts": attempts}
                    continue
                return {"status": ERROR, "error": str(exc),
                        "ms": self._ms(started), "attempts": attempts}
            except Exception as exc:
                log.exception("Нода %s упала", node.title)
                if attempts <= options.retries and not context.expired():
                    try:
                        context.sleep(0.2)
                    except RunAborted as aborted:
                        self._stop(result, aborted)
                        return {"status": SKIPPED, "ms": self._ms(started),
                                "reason": aborted.message, "attempts": attempts}
                    continue
                return {"status": ERROR, "error": f"{type(exc).__name__}: {exc}",
                        "ms": self._ms(started), "attempts": attempts}

            result.results[node_id] = outputs
            entry = {"status": DONE, "ms": self._ms(started), "attempts": attempts}
            if node.inputs and any(value is not None for value in inputs.values()):
                if "in" in inputs and inputs["in"] is None:
                    entry["warning"] = "на вход пришло пустое значение"
            return entry

    @staticmethod
    def _ms(started):
        return round((time.perf_counter() - started) * 1000, 1)


def run_graph(graph, seed=None, options=None, progress=None, cancel=None):
    return Engine().run(graph, seed, options, progress, cancel)


def prepare_demo():
    """Демо-граф: ввод данных -> фильтр -> условие -> журнал / просмотр."""
    g = Graph()
    source = g.add_node("source.input", (0, 0))
    filt = g.add_node("data.filter", (280, 0))
    cond = g.add_node("logic.condition", (560, 0))
    journal = g.add_node("action.log", (860, -90))
    dbg = g.add_node("output.debug", (860, 90))

    source.set_param("json", '{"name":"Мария","status":"новая","balance":-3}')
    filt.set_param("field", "status")
    filt.set_param("op", "==")
    filt.set_param("value", "новая")
    cond.set_param("field", "balance")
    cond.set_param("op", "<")
    cond.set_param("value", "0")
    journal.set_param("label", "долг клиента")

    g.add_edge(source.node_id, "out", filt.node_id, "in")
    g.add_edge(filt.node_id, "pass", cond.node_id, "in")
    g.add_edge(cond.node_id, "true", journal.node_id, "in")
    g.add_edge(cond.node_id, "false", dbg.node_id, "in")

    return g


if __name__ == "__main__":
    graph = prepare_demo()
    seed = {"trigger.manual": {"status": "новая", "balance": -5, "name": "Анна"}}
    outcome = Engine().run(graph, seed)
    for node_id, outputs in outcome.results.items():
        print(graph.nodes[node_id].title, "->", outputs)
    for node_id, info in outcome.report.items():
        print(graph.nodes[node_id].title, info)
    print(outcome.summary)
