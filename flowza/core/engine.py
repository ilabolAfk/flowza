"""Движок выполнения графа: интерпретирует модель (ноды + рёбра).

Не зависит от UI. Обходит все ноды в топологическом порядке и возвращает
результат по каждой ноде плюс отчёт: выполнено / пропущено / ошибка / цикл.
"""

import logging
import time
from dataclasses import dataclass, field

from ..model.graph import Graph, find_cycles
from ..model.registry import NodeError, run_node

log = logging.getLogger("flowza")

DONE = "done"
SKIPPED = "skipped"
ERROR = "error"
CYCLE = "cycle"


@dataclass
class RunResult:
    """Результат прогона: значения по нодам + отчёт о статусах."""

    results: dict = field(default_factory=dict)
    report: dict = field(default_factory=dict)
    order: list = field(default_factory=list)

    @property
    def ok(self):
        return not any(item["status"] == ERROR for item in self.report.values())

    @property
    def summary(self):
        counts = {DONE: 0, SKIPPED: 0, ERROR: 0, CYCLE: 0}
        for item in self.report.values():
            counts[item["status"]] += 1
        return counts

    def to_dict(self):
        return {"ok": self.ok, "result": self.results, "report": self.report,
                "order": self.order, "summary": self.summary}


def topological_order(graph):
    """Порядок выполнения + список нод, застрявших в циклах."""
    indegree = {node_id: 0 for node_id in graph.nodes}
    children = {node_id: [] for node_id in graph.nodes}
    for edge in graph.edges:
        if edge.source_id in graph.nodes and edge.target_id in graph.nodes:
            indegree[edge.target_id] += 1
            children[edge.source_id].append(edge.target_id)

    queue = [node_id for node_id in graph.nodes if indegree[node_id] == 0]
    order = []
    index = 0
    while index < len(queue):
        current = queue[index]
        index += 1
        order.append(current)
        for child in children[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    cycles = [node_id for node_id, degree in indegree.items() if degree > 0]
    return order, cycles


class Engine:
    """Топологический проход по всему графу с отчётом по каждой ноде."""

    def run(self, graph, seed=None):
        """seed: {node_id или node_type: стартовое значение}."""
        seed = seed or {}
        result = RunResult()

        order, cycles = topological_order(graph)
        result.order = order

        for node_id in order:
            node = graph.nodes[node_id]
            started = time.perf_counter()
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

            entry = {"status": DONE, "ms": 0}

            if broken_parent:
                entry.update(status=SKIPPED,
                             reason=f"ветка не выполнилась: «{broken_parent}»")
                result.report[node_id] = entry
                continue

            if node.inputs and not edge_list:
                entry.update(status=SKIPPED, reason="вход ни к чему не подключён")
                result.report[node_id] = entry
                continue

            if edge_list and all(value is None for value in inputs.values()):
                entry.update(status=SKIPPED, reason="нет данных: ветка не активна")
                result.report[node_id] = entry
                continue

            if not edge_list:
                # источник: значение из seed по id ноды, затем по типу
                value = seed.get(node_id, seed.get(node.node_type))
                inputs["_seed"] = value if value is not None \
                    else {"event": node.node_type}

            try:
                outputs = run_node(node, inputs)
            except NodeError as exc:
                entry.update(status=ERROR, error=str(exc))
            except Exception as exc:  # непредвиденная ошибка — не роняем весь прогон
                log.exception("Нода %s упала", node.title)
                entry.update(status=ERROR, error=f"{type(exc).__name__}: {exc}")
            else:
                result.results[node_id] = outputs
                if node.inputs and any(value is not None for value in inputs.values()):
                    if "in" in inputs and inputs["in"] is None:
                        entry["warning"] = "на вход пришло пустое значение"

            entry["ms"] = round((time.perf_counter() - started) * 1000, 1)
            result.report[node_id] = entry

        for node_id in cycles:
            result.report[node_id] = {"status": CYCLE, "ms": 0,
                                      "reason": "нода участвует в цикле"}
        return result


def run_graph(graph, seed=None):
    return Engine().run(graph, seed)


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
