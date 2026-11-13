"""Тесты движка, модели графа и реестра нод.

Запуск из корня проекта: python -m unittest discover -s tests -v
"""

import os
import unittest
from unittest import mock

from flowza.core.engine import DONE, ERROR, SKIPPED, CYCLE, Engine, prepare_demo
from flowza.model.graph import Graph
from flowza.model.registry import NodeError, compare, evaluate, run_node


def chain(graph, node_type, params=None, source_port="out"):
    """trigger.manual -> node, вернуть ноду."""
    src = graph.add_node("trigger.manual")
    node = graph.add_node(node_type)
    for key, value in (params or {}).items():
        node.set_param(key, value)
    graph.add_edge(src.node_id, source_port, node.node_id, "in")
    return src, node


class EngineTests(unittest.TestCase):
    def test_isolated_node_is_skipped_with_reason(self):
        graph = Graph()
        debug = graph.add_node("output.debug")
        outcome = Engine().run(graph)
        self.assertEqual(outcome.report[debug.node_id]["status"], SKIPPED)
        self.assertIn("подключён", outcome.report[debug.node_id]["reason"])

    def test_all_nodes_are_visited(self):
        graph = Graph()
        first = graph.add_node("action.log")
        second = graph.add_node("action.log")
        outcome = Engine().run(graph)
        self.assertIn(first.node_id, outcome.report)
        self.assertIn(second.node_id, outcome.report)

    def test_inactive_branch_is_skipped(self):
        graph = Graph()
        src, cond = chain(graph, "logic.condition",
                          {"field": "balance", "op": "<", "value": "0"})
        log = graph.add_node("action.log")
        graph.add_edge(cond.node_id, "true", log.node_id, "in")
        outcome = Engine().run(graph, {src.node_id: {"balance": 10}})
        self.assertEqual(outcome.report[log.node_id]["status"], SKIPPED)
        self.assertIn("ветка", outcome.report[log.node_id]["reason"])

    def test_active_branch_runs(self):
        graph = Graph()
        src, cond = chain(graph, "logic.condition",
                          {"field": "balance", "op": "<", "value": "0"})
        log = graph.add_node("action.log")
        graph.add_edge(cond.node_id, "true", log.node_id, "in")
        outcome = Engine().run(graph, {src.node_id: {"balance": -5}})
        self.assertEqual(outcome.report[log.node_id]["status"], DONE)

    def test_error_stops_the_branch(self):
        graph = Graph()
        source = graph.add_node("source.input")
        source.set_param("json", "{не json}")
        log = graph.add_node("action.log")
        graph.add_edge(source.node_id, "out", log.node_id, "in")
        outcome = Engine().run(graph)
        self.assertEqual(outcome.report[source.node_id]["status"], ERROR)
        self.assertIn("JSON", outcome.report[source.node_id]["error"])
        self.assertEqual(outcome.report[log.node_id]["status"], SKIPPED)
        self.assertFalse(outcome.ok)

    def test_cycle_is_reported(self):
        graph = Graph()
        first = graph.add_node("logic.condition")
        second = graph.add_node("logic.condition")
        graph.add_edge(first.node_id, "true", second.node_id, "in")
        graph.add_edge(second.node_id, "false", first.node_id, "in")
        outcome = Engine().run(graph)
        self.assertEqual(outcome.report[first.node_id]["status"], CYCLE)
        self.assertEqual(outcome.report[second.node_id]["status"], CYCLE)

    def test_demo_graph(self):
        graph = prepare_demo()
        outcome = Engine().run(graph)
        self.assertEqual(outcome.summary[DONE], 4)
        self.assertEqual(outcome.summary[SKIPPED], 1)
        self.assertTrue(outcome.ok)

    def test_topological_order(self):
        graph = Graph()
        src, filt = chain(graph, "data.filter")
        cond = graph.add_node("logic.condition")
        graph.add_edge(filt.node_id, "pass", cond.node_id, "in")
        outcome = Engine().run(graph)
        self.assertLess(outcome.order.index(src.node_id), outcome.order.index(filt.node_id))
        self.assertLess(outcome.order.index(filt.node_id), outcome.order.index(cond.node_id))


class RegistryTests(unittest.TestCase):
    def test_compare_numbers_and_strings(self):
        self.assertTrue(compare(-5, "==", "-5"))
        self.assertTrue(compare("1 234,50", "==", "1234.5"))
        self.assertTrue(compare("Новая", "==", "новая"))
        self.assertFalse(compare(10, "<", "9"))
        self.assertTrue(compare(None, "пусто", ""))
        self.assertTrue(compare([1, 2], "содержит", "2"))
        self.assertTrue(compare("привет", "содержит", "ВЕТ"))
        with self.assertRaises(NodeError):
            compare("abc", ">", "1")

    def test_evaluate_precedence(self):
        self.assertEqual(evaluate("2 + 3 * 4", {}), 14)
        self.assertEqual(evaluate("price * qty", {"price": 10, "qty": 3}), 30)
        self.assertEqual(evaluate("(a + b) * 2", {"a": 1, "b": 2}), 6)

    def test_evaluate_errors(self):
        with self.assertRaises(NodeError):
            evaluate("1 / 0", {})
        with self.assertRaises(NodeError):
            evaluate("missing * 2", {"price": 1})
        with self.assertRaises(NodeError):
            evaluate("price * qty", {"price": "дорого", "qty": 2})
        with self.assertRaises(NodeError):
            evaluate("", {})

    def test_filter_compares_numbers(self):
        graph = Graph()
        src, filt = chain(graph, "data.filter",
                          {"field": "balance", "op": ">=", "value": "0"})
        outcome = Engine().run(graph, {src.node_id: {"balance": -5}})
        self.assertIsNone(outcome.results[filt.node_id]["pass"])
        self.assertEqual(outcome.results[filt.node_id]["drop"], {"balance": -5})

    def test_filter_reports_missing_field(self):
        graph = Graph()
        src, filt = chain(graph, "data.filter", {"field": "nope"})
        outcome = Engine().run(graph, {src.node_id: {"balance": -5}})
        self.assertEqual(outcome.report[filt.node_id]["status"], ERROR)
        self.assertIn("nope", outcome.report[filt.node_id]["error"])

    def test_map_and_calculate_write_values(self):
        graph = Graph()
        src, mapper = chain(graph, "data.map", {"formula": "price * qty"})
        outcome = Engine().run(graph, {src.node_id: {"price": 10, "qty": 3}})
        self.assertEqual(outcome.results[mapper.node_id]["out"], 30)

        graph = Graph()
        src, calc = chain(graph, "data.calculate",
                          {"out_field": "total", "expr": "a - b - c"})
        outcome = Engine().run(graph, {src.node_id: {"a": 10, "b": 2, "c": 3}})
        self.assertEqual(outcome.results[calc.node_id]["out"]["total"], 5)

    def test_wait_accepts_empty_seconds(self):
        graph = Graph()
        src, wait = chain(graph, "logic.wait", {"seconds": ""})
        outcome = Engine().run(graph, {src.node_id: "данные"})
        self.assertEqual(outcome.report[wait.node_id]["status"], DONE)

    def test_merge_dicts(self):
        graph = Graph()
        first = graph.add_node("source.input")
        first.set_param("json", '{"a": 1}')
        second = graph.add_node("source.input")
        second.set_param("json", '{"b": 2}')
        merge = graph.add_node("logic.merge")
        graph.add_edge(first.node_id, "out", merge.node_id, "a")
        graph.add_edge(second.node_id, "out", merge.node_id, "b")
        outcome = Engine().run(graph)
        self.assertEqual(outcome.results[merge.node_id]["out"], {"a": 1, "b": 2})

    def test_mail_without_server_reports_error(self):
        graph = Graph()
        src, mail = chain(graph, "action.mail")
        mail.set_param("smtp_host", "")
        with mock.patch.dict(os.environ, {"FLOWZA_SMTP_HOST": ""}, clear=False):
            outcome = Engine().run(graph, {src.node_id: {"name": "Анна"}})
        self.assertEqual(outcome.report[mail.node_id]["status"], ERROR)
        self.assertIn("SMTP", outcome.report[mail.node_id]["error"])

    def test_webhook_rejects_bad_url(self):
        graph = Graph()
        src, hook = chain(graph, "action.webhook", {"url": "ftp://example.com"})
        outcome = Engine().run(graph, {src.node_id: {"a": 1}})
        self.assertEqual(outcome.report[hook.node_id]["status"], ERROR)
        self.assertIn("http", outcome.report[hook.node_id]["error"])

    def test_ai_node_is_marked_as_stub(self):
        graph = Graph()
        src, ai = chain(graph, "action.ai", {"prompt": "привет, {{name}}"})
        outcome = Engine().run(graph, {src.node_id: {"name": "Анна"}})
        value = outcome.results[ai.node_id]["out"]
        self.assertTrue(value["stub"])
        self.assertIn("Анна", value["prompt"])

    def test_run_node_rejects_unknown_type(self):
        class Fake:
            node_type = "nope.nope"
            params = {}

        with self.assertRaises(NodeError):
            run_node(Fake(), {})


class GraphTests(unittest.TestCase):
    def test_self_loop_is_rejected(self):
        graph = Graph()
        node = graph.add_node("logic.condition")
        with self.assertRaises(ValueError):
            graph.add_edge(node.node_id, "true", node.node_id, "in")

    def test_one_edge_per_input(self):
        graph = Graph()
        first = graph.add_node("trigger.manual")
        second = graph.add_node("trigger.manual")
        target = graph.add_node("output.debug")
        graph.add_edge(first.node_id, "out", target.node_id, "in")
        graph.add_edge(second.node_id, "out", target.node_id, "in")
        self.assertEqual(len(graph.edges), 1)
        self.assertEqual(graph.edges[0].source_id, second.node_id)

    def test_unknown_node_type_is_skipped_with_warning(self):
        graph = Graph.from_dict({"nodes": [{"id": "x", "type": "nope"}], "edges": []})
        self.assertEqual(len(graph.nodes), 0)
        self.assertTrue(graph.load_warnings)

    def test_dangling_edge_is_skipped(self):
        graph = Graph.from_dict({
            "nodes": [{"id": "a", "type": "trigger.manual"}],
            "edges": [{"source": "a", "source_port": "out",
                       "target": "ghost", "target_port": "in"}],
        })
        self.assertEqual(len(graph.edges), 0)
        self.assertTrue(graph.load_warnings)

    def test_validate_finds_cycle_and_unconnected_input(self):
        graph = Graph()
        first = graph.add_node("logic.condition")
        second = graph.add_node("logic.condition")
        graph.add_edge(first.node_id, "true", second.node_id, "in")
        graph.add_edge(second.node_id, "false", first.node_id, "in")
        issues = graph.validate()
        levels = {issue["level"] for issue in issues}
        self.assertIn("error", levels)

    def test_roundtrip_preserves_params(self):
        graph = Graph()
        node = graph.add_node("action.webhook")
        node.set_param("url", "https://example.com/x")
        restored = Graph.from_dict(graph.to_dict())
        copy = next(iter(restored.nodes.values()))
        self.assertEqual(copy.params["url"], "https://example.com/x")


if __name__ == "__main__":
    unittest.main()
