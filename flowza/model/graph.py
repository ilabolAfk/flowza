"""Модель графа: ноды, порты, рёбра и сериализация в JSON.

Граф хранится как данные (JSON), а не как программа — его интерпретирует движок.
"""

import json
import math
import re
import uuid

from ..security import MAX_GRAPH_EDGES, MAX_GRAPH_NODES, MAX_PARAM_CHARS
from .registry import node_spec

_ID_CLEAN = re.compile(r"[^A-Za-z0-9_-]")
MAX_PARAM_KEY = 64
MAX_PARAMS = 50
COORD_LIMIT = 100_000


def _coord(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(number):
        return 0
    return int(max(-COORD_LIMIT, min(COORD_LIMIT, number)))


def _clean_params(raw, warnings, node_id):
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        warnings.append(f"нода {node_id}: параметры должны быть объектом")
        return {}
    clean = {}
    for key, value in list(raw.items())[:MAX_PARAMS * 2]:
        name = str(key)[:MAX_PARAM_KEY]
        if isinstance(value, (bool, int, float)) or value is None:
            if isinstance(value, float) and not math.isfinite(value):
                continue
            clean[name] = value
        elif isinstance(value, str):
            clean[name] = value[:MAX_PARAM_CHARS]
        else:
            warnings.append(f"нода {node_id}: параметр {name} пропущен (сложный тип)")
    return clean


class Port:
    """Порт ноды (пин): вход или выход."""

    def __init__(self, name, kind="data", label="", direction="input"):
        self.name = name
        self.kind = kind          # "data" | "control"
        self.label = label or name
        self.direction = direction  # "input" | "output"

    def to_dict(self):
        return {"name": self.name, "kind": self.kind, "label": self.label,
                "direction": self.direction}


class Node:
    """Экземпляр ноды на канвасе."""

    def __init__(self, node_type, pos=None, node_id=None):
        spec = node_spec(node_type)
        if spec is None:
            raise ValueError(f"Неизвестный тип ноды: {node_type}")
        self.node_id = node_id or uuid.uuid4().hex[:8]
        self.node_type = node_type
        self.title = spec["title"]
        self.pos = (_coord(pos[0]), _coord(pos[1])) if pos else (0, 0)
        # Параметры формы со значениями по умолчанию из реестра
        self.params = {}
        for p in spec.get("params", []):
            self.params[p["name"]] = p.get("default", "")

        self.inputs = [Port(p["name"], p.get("kind", "data"), p.get("label", ""), "input")
                       for p in spec.get("inputs", [])]
        self.outputs = [Port(p["name"], p.get("kind", "data"), p.get("label", ""), "output")
                        for p in spec.get("outputs", [])]

    def set_param(self, name, value):
        if name in self.params:
            self.params[name] = value

    def input_names(self):
        return [p.name for p in self.inputs]

    def output_names(self):
        return [p.name for p in self.outputs]

    def to_dict(self):
        return {"id": self.node_id,
                "type": self.node_type,
                "title": self.title,
                "x": self.pos[0],
                "y": self.pos[1],
                "params": dict(self.params)}


class Edge:
    """Соединение: из одного выходного порта в один входной."""

    def __init__(self, source_id, source_port, target_id, target_port, edge_id=None):
        self.edge_id = edge_id or uuid.uuid4().hex[:8]
        self.source_id = source_id
        self.source_port = source_port
        self.target_id = target_id
        self.target_port = target_port

    def to_dict(self):
        return {"id": self.edge_id,
                "source": self.source_id, "source_port": self.source_port,
                "target": self.target_id, "target_port": self.target_port}


class Graph:
    """Контейнер нод и рёбер с проверкой целостности."""

    def __init__(self):
        self.nodes = {}   # node_id -> Node
        self.edges = []   # list[Edge]
        self.load_warnings = []

    def add_node(self, node_type, pos=None, node_id=None):
        node = Node(node_type, pos, node_id)
        if node.node_id in self.nodes:
            node.node_id = uuid.uuid4().hex[:8]
        self.nodes[node.node_id] = node
        return node

    def remove_node(self, node_id):
        if node_id in self.nodes:
            del self.nodes[node_id]
        self.edges = [e for e in self.edges
                      if e.source_id != node_id and e.target_id != node_id]

    def add_edge(self, source_id, source_port, target_id, target_port):
        # проверка существования портов
        src = self.nodes.get(source_id)
        tgt = self.nodes.get(target_id)
        if not src or not tgt:
            raise ValueError("Нода не найдена при создании ребра")
        if source_id == target_id:
            raise ValueError(f"«{src.title}» нельзя соединить саму с собой")
        if source_port not in src.output_names():
            raise ValueError(f"Нет выходного порта '{source_port}' у {src.title}")
        if target_port not in tgt.input_names():
            raise ValueError(f"Нет входного порта '{target_port}' у {tgt.title}")
        self._drop_existing_input(target_id, target_port)
        self.edges.append(Edge(source_id, source_port, target_id, target_port))
        return self.edges[-1]

    def _drop_existing_input(self, target_id, target_port):
        """Один входной порт принимает только одно ребро."""
        self.edges = [e for e in self.edges
                      if not (e.target_id == target_id and e.target_port == target_port)]

    def remove_edge(self, edge_id):
        self.edges = [e for e in self.edges if e.edge_id != edge_id]

    def to_dict(self):
        return {"nodes": [n.to_dict() for n in self.nodes.values()],
                "edges": [e.to_dict() for e in self.edges]}

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data):
        g = cls()
        if not isinstance(data, dict):
            g.load_warnings.append("граф должен быть объектом")
            return g
        nodes = data.get("nodes", [])
        edges = data.get("edges", [])
        if not isinstance(nodes, list) or not isinstance(edges, list):
            g.load_warnings.append("nodes и edges должны быть массивами")
            return g
        if len(nodes) > MAX_GRAPH_NODES:
            g.load_warnings.append(
                f"слишком много нод: загружены первые {MAX_GRAPH_NODES}")
        if len(edges) > MAX_GRAPH_EDGES:
            g.load_warnings.append(
                f"слишком много связей: загружены первые {MAX_GRAPH_EDGES}")

        for payload in nodes[:MAX_GRAPH_NODES]:
            if not isinstance(payload, dict):
                g.load_warnings.append("нода пропущена: ожидался объект")
                continue
            node_type = payload.get("type")
            if not isinstance(node_type, str) or len(node_type) > 64:
                g.load_warnings.append("нода пропущена: некорректный тип")
                continue
            raw_id = payload.get("id")
            node_id = None
            if raw_id is not None:
                node_id = _ID_CLEAN.sub("", str(raw_id))[:40] or None
            try:
                node = g.add_node(node_type, (payload.get("x", 0), payload.get("y", 0)),
                                  node_id)
            except (ValueError, KeyError, TypeError) as exc:
                g.load_warnings.append(f"нода пропущена: {exc}")
                continue
            node.params.update(_clean_params(payload.get("params"), g.load_warnings,
                                             node.node_id))

        for payload in edges[:MAX_GRAPH_EDGES]:
            if not isinstance(payload, dict):
                g.load_warnings.append("связь пропущена: ожидался объект")
                continue
            try:
                source = str(payload["source"])
                target = str(payload["target"])
                source_port = str(payload["source_port"])
                target_port = str(payload["target_port"])
                g.add_edge(source, source_port, target, target_port)
            except (KeyError, ValueError, TypeError) as exc:
                g.load_warnings.append(f"связь пропущена: {exc}")
                continue
            edge_id = payload.get("id")
            if edge_id:
                g.edges[-1].edge_id = _ID_CLEAN.sub("", str(edge_id))[:40]
        return g

    def source_nodes(self):
        """Ноды без входных портов — естественные точки старта."""
        return [n for n in self.nodes.values() if not n.inputs]

    def outgoing(self, node_id):
        return [e for e in self.edges if e.source_id == node_id]

    def incoming(self, node_id):
        return [e for e in self.edges if e.target_id == node_id]

    def validate(self):
        """Список замечаний к графу: [{"level": "error"|"warn", "message": str}]."""
        issues = []
        for edge in self.edges:
            if edge.source_id not in self.nodes or edge.target_id not in self.nodes:
                issues.append({"level": "error",
                               "message": f"связь {edge.edge_id} ссылается на удалённую ноду"})
                continue
            src = self.nodes[edge.source_id]
            tgt = self.nodes[edge.target_id]
            if edge.source_port not in src.output_names():
                issues.append({"level": "error",
                               "message": f"у «{src.title}» нет выхода «{edge.source_port}»"})
            if edge.target_port not in tgt.input_names():
                issues.append({"level": "error",
                               "message": f"у «{tgt.title}» нет входа «{edge.target_port}»"})

        for node_id in find_cycles(self):
            issues.append({"level": "error", "node_id": node_id,
                           "message": f"«{self.nodes[node_id].title}» участвует в цикле"})

        for node in self.nodes.values():
            if node.inputs and not self.incoming(node.node_id):
                issues.append({"level": "warn", "node_id": node.node_id,
                               "message": f"«{node.title}»: вход ни к чему не подключён"})
        return issues


def topological_order(graph):
    """Порядок выполнения нод и идентификаторы нод, застрявших в циклах."""
    indegree = {node_id: 0 for node_id in graph.nodes}
    children = {node_id: [] for node_id in graph.nodes}
    for edge in graph.edges:
        if edge.source_id in graph.nodes and edge.target_id in graph.nodes:
            indegree[edge.target_id] += 1
            children[edge.source_id].append(edge.target_id)

    queue = [node_id for node_id, degree in indegree.items() if degree == 0]
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


def find_cycles(graph):
    """Идентификаторы нод, которые остались за топологической сортировкой."""
    return topological_order(graph)[1]
