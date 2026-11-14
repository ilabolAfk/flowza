"""Тесты настроек, безопасности, новых нод, движка, планировщика и API.

Запуск из корня проекта: python -m unittest discover -s tests -v
"""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from flowza import security, settings as settings_module
from flowza.core.engine import (DONE, ERROR, SKIPPED, Engine, RunOptions,
                                topological_order)
from flowza.core.scheduler import Scheduler
from flowza.model.graph import Graph
from flowza.model.registry import NodeError, run_node


def chain(graph, node_type, params=None, source_port="out"):
    src = graph.add_node("trigger.manual")
    node = graph.add_node(node_type)
    for key, value in (params or {}).items():
        node.set_param(key, value)
    graph.add_edge(src.node_id, source_port, node.node_id, "in")
    return src, node


def feed(graph, node_type, data, params=None):
    """source.input с JSON → нода."""
    src = graph.add_node("source.input")
    src.set_param("json", json.dumps(data, ensure_ascii=False))
    node = graph.add_node(node_type)
    for key, value in (params or {}).items():
        node.set_param(key, value)
    graph.add_edge(src.node_id, "out", node.node_id, "in")
    return src, node


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_safe_path_accepts_file_inside_base(self):
        (self.base / "ok.json").write_text("{}", encoding="utf-8")
        path = security.safe_path(self.base, "ok.json")
        self.assertTrue(path.is_file())

    def test_safe_path_blocks_traversal_and_absolute(self):
        for raw in ("../secret.txt", "..\\secret.txt", "a/../../x", "C:\\Windows\\win.ini",
                    "/etc/passwd", "", ".", "con.txt", "aux.json"):
            with self.assertRaises(security.SecurityError, msg=raw):
                security.safe_path(self.base, raw, for_write=True)

    def test_safe_path_blocks_symlink_escape(self):
        outside = Path(tempfile.mkdtemp())
        (outside / "data.json").write_text("{}", encoding="utf-8")
        link = self.base / "link.json"
        try:
            link.symlink_to(outside / "data.json")
        except (OSError, NotImplementedError):
            self.skipTest("симлинки недоступны")
        with self.assertRaises(security.SecurityError):
            security.safe_path(self.base, "link.json")

    def test_safe_path_limits_write_extension(self):
        with self.assertRaises(security.SecurityError):
            security.safe_path(self.base, "payload.exe", for_write=True)

    def test_safe_path_requires_existing_file_for_read(self):
        with self.assertRaises(security.SecurityError):
            security.safe_path(self.base, "missing.json")

    def test_safe_url_blocks_private_targets(self):
        for url in ("http://127.0.0.1/x", "http://localhost/x",
                    "http://169.254.169.254/latest/meta-data/",
                    "http://10.0.0.5/x", "http://192.168.1.1/x",
                    "http://[::1]/x", "file:///etc/passwd",
                    "ftp://example.com/x", "http://user:pass@example.com/x",
                    "http://example.com:0/x"):
            with self.assertRaises(security.SecurityError, msg=url):
                security.safe_url(url)

    def test_safe_url_allows_private_when_enabled(self):
        self.assertEqual(security.safe_url("http://127.0.0.1:8080/x",
                                           allow_private=True),
                         "http://127.0.0.1:8080/x")

    def test_header_text_rejects_newlines(self):
        with self.assertRaises(security.SecurityError):
            security.header_text("Тема\r\nBcc: evil@example.com")

    def test_parse_emails_rejects_injection(self):
        self.assertEqual(security.parse_emails("a@example.ru; b@example.ru"),
                         ["a@example.ru", "b@example.ru"])
        for raw in ("a@example.ru\r\nBcc: x@example.com", "не адрес"):
            with self.assertRaises(security.SecurityError, msg=raw):
                security.parse_emails(raw)

    def test_check_json_limits_depth(self):
        deep = current = {}
        for _ in range(60):
            current["a"] = {}
            current = current["a"]
        with self.assertRaises(security.SecurityError):
            security.check_json(deep)
        security.check_json({"a": [1, 2, 3]})

    def test_compile_regex_blocks_nested_quantifiers(self):
        for pattern in ("(a+)+b", "(a*)*", "(\\d+){2,}"):
            with self.assertRaises(security.SecurityError, msg=pattern):
                security.compile_regex(pattern)
        self.assertTrue(security.compile_regex(r"\d+"))
        with self.assertRaises(security.SecurityError):
            security.compile_regex("a" * 600)

    def test_csv_cell_escapes_formulas(self):
        self.assertEqual(security.csv_cell("=1+1"), "'=1+1")
        self.assertEqual(security.csv_cell("@cmd"), "'@cmd")
        self.assertEqual(security.csv_cell("обычный"), "обычный")

    def test_token_ok(self):
        self.assertTrue(security.token_ok("secret", "secret"))
        self.assertFalse(security.token_ok("secret", "other"))
        self.assertFalse(security.token_ok("secret", ""))

    def test_rate_limiter(self):
        limiter = security.RateLimiter(2, 60)
        self.assertTrue(limiter.allow("a"))
        self.assertTrue(limiter.allow("a"))
        self.assertFalse(limiter.allow("a"))
        self.assertTrue(limiter.allow("b"))

    def test_origin_host(self):
        self.assertEqual(security.origin_host("http://127.0.0.1:8765"), "127.0.0.1")
        self.assertIsNone(security.origin_host(""))
        self.assertIsNone(security.origin_host("не url"))


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "settings.json"
        settings_module._current = None

    def tearDown(self):
        settings_module._current = None
        self.tmp.cleanup()

    def test_defaults_cover_sections(self):
        data = settings_module.defaults()
        for section in ("run", "files", "mail", "ui", "webhook", "security"):
            self.assertIn(section, data)

    def test_normalize_clamps_and_warns(self):
        clean, warnings = settings_module.normalize({
            "run": {"pause_ms": 999999, "on_error": "взлом", "retries": 99},
            "files": {"autosave_sec": -5},
        })
        self.assertEqual(clean["run"]["pause_ms"], 5000)
        self.assertEqual(clean["run"]["retries"], 3)
        self.assertEqual(clean["run"]["on_error"], "continue")
        self.assertEqual(clean["files"]["autosave_sec"], 0)
        self.assertTrue(warnings)

    def test_normalize_drops_unknown_keys(self):
        clean, warnings = settings_module.normalize({
            "run": {"unknown_key": 1},
            "hacker": {"x": 1},
        })
        self.assertNotIn("unknown_key", clean["run"])
        self.assertNotIn("hacker", clean)
        self.assertTrue(any("unknown_key" in item for item in warnings))

    def test_save_and_load_roundtrip(self):
        clean = settings_module.save({"ui": {"theme": "light", "grid": 32}},
                                     self.path)
        self.assertEqual(clean["ui"]["theme"], "light")
        loaded, warnings = settings_module.load(self.path)
        self.assertEqual(loaded["ui"]["grid"], 32)
        self.assertEqual(warnings, [])

    def test_public_hides_secret_values(self):
        with mock.patch.dict(os.environ, {"FLOWZA_SMTP_PASSWORD": "top-secret"}):
            payload = settings_module.public(settings_module.defaults())
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("top-secret", text)
        self.assertTrue(payload["env"]["smtp_password_set"])

    def test_workspace_falls_back_to_default(self):
        data = settings_module.defaults()
        data["files"]["workspace"] = ""
        self.assertEqual(settings_module.workspace(data),
                         settings_module.default_workspace())


class NodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        clean, _ = settings_module.normalize({"files": {"workspace": str(self.base)}})
        settings_module.set_current(clean)

    def tearDown(self):
        settings_module._current = None
        self.tmp.cleanup()

    def test_write_and_read_json(self):
        graph = Graph()
        src = graph.add_node("source.input")
        src.set_param("json", '{"a": 1, "b": [1, 2]}')
        writer = graph.add_node("io.file.write")
        writer.set_param("path", "out.json")
        writer.set_param("format", "json")
        graph.add_edge(src.node_id, "out", writer.node_id, "in")
        outcome = Engine().run(graph)
        self.assertEqual(outcome.report[writer.node_id]["status"], DONE)
        self.assertEqual(json.loads((self.base / "out.json").read_text("utf-8")),
                         {"a": 1, "b": [1, 2]})

        graph = Graph()
        reader = graph.add_node("io.file.read")
        reader.set_param("path", "out.json")
        reader.set_param("format", "json")
        outcome = Engine().run(graph)
        self.assertEqual(outcome.results[reader.node_id]["out"]["a"], 1)

    def test_file_write_blocks_traversal_and_bad_extension(self):
        for path in ("../escape.json", "payload.exe", "C:\\temp\\x.json"):
            graph = Graph()
            _, writer = feed(graph, "io.file.write", {"a": 1},
                             {"path": path, "format": "json"})
            outcome = Engine().run(graph)
            self.assertEqual(outcome.report[writer.node_id]["status"], ERROR, path)

    def test_csv_write_neutralises_formula(self):
        graph = Graph()
        _, writer = feed(graph, "io.file.write", {"name": "=1+1"},
                         {"path": "out.csv", "format": "csv", "delimiter": ";"})
        outcome = Engine().run(graph)
        self.assertEqual(outcome.report[writer.node_id]["status"], DONE)
        self.assertIn("'=1+1", (self.base / "out.csv").read_text("utf-8"))

    def test_foreach_and_aggregate(self):
        graph = Graph()
        _, loop = feed(graph, "data.foreach", {"items": [{"n": 2}, {"n": 3}]},
                       {"field": "items", "formula": "n * 2", "out_field": "v"})
        outcome = Engine().run(graph)
        self.assertEqual(outcome.report[loop.node_id]["status"], DONE)
        values = [item["v"] for item in outcome.results[loop.node_id]["out"]]
        self.assertEqual(values, [4, 6])

        for op, expected in (("сумма", 6), ("количество", 3), ("среднее", 2)):
            graph = Graph()
            _, agg = feed(graph, "data.aggregate", {"items": [1, 2, 3]},
                          {"field": "items", "op": op})
            outcome = Engine().run(graph)
            self.assertEqual(outcome.report[agg.node_id]["status"], DONE)
            self.assertEqual(outcome.results[agg.node_id]["out"], expected)

    def test_regex_extract_replace_and_guard(self):
        graph = Graph()
        _, node = feed(graph, "data.regex", {"text": "abc123"},
                       {"field": "text", "pattern": r"\d+", "mode": "извлечь",
                        "out_field": "match"})
        outcome = Engine().run(graph)
        self.assertEqual(outcome.results[node.node_id]["out"]["match"], "123")

        graph = Graph()
        _, node = feed(graph, "data.regex", {"text": "aaa"},
                       {"field": "text", "pattern": "a", "mode": "заменить",
                        "replacement": "b", "out_field": "match"})
        outcome = Engine().run(graph)
        self.assertEqual(outcome.results[node.node_id]["out"]["match"], "bbb")

        graph = Graph()
        _, node = feed(graph, "data.regex", {"text": "aaa"},
                       {"field": "text", "pattern": "(a+)+", "mode": "извлечь"})
        outcome = Engine().run(graph)
        self.assertEqual(outcome.report[node.node_id]["status"], ERROR)
        self.assertIn("квантификатор", outcome.report[node.node_id]["error"])

    def test_datetime_parse_and_shift(self):
        graph = Graph()
        _, node = feed(graph, "data.datetime",
                       {"created_at": "2024-01-31 10:00:00"},
                       {"field": "created_at", "action": "сдвинуть",
                        "format": "%Y-%m-%d %H:%M:%S", "amount": 1,
                        "unit": "дней", "out_field": "created_at"})
        outcome = Engine().run(graph)
        self.assertEqual(outcome.report[node.node_id]["status"], DONE)
        self.assertIn("2024-02-01", str(outcome.results[node.node_id]["out"]))

    def test_sqlite_create_insert_select(self):
        graph = Graph()
        _, create = feed(graph, "sqlite.query", {}, {
            "db": "t.db", "sql": "CREATE TABLE t (a INTEGER)", "allow_write": True})
        outcome = Engine().run(graph)
        self.assertEqual(outcome.report[create.node_id]["status"], DONE)

        _, insert = feed(graph, "sqlite.query", {}, {
            "db": "t.db", "sql": "INSERT INTO t (a) VALUES (?)",
            "params": "[5]", "allow_write": True})
        outcome = Engine().run(graph)
        self.assertEqual(outcome.report[insert.node_id]["status"], DONE)

        _, select = feed(graph, "sqlite.query", {}, {
            "db": "t.db", "sql": "SELECT a FROM t"})
        outcome = Engine().run(graph)
        self.assertEqual(outcome.report[select.node_id]["status"], DONE)
        self.assertIn("5", json.dumps(outcome.results[select.node_id]["out"],
                                      ensure_ascii=False, default=str))

    def test_sqlite_blocks_write_by_default_and_multistatement(self):
        graph = Graph()
        _, node = feed(graph, "sqlite.query", {}, {
            "db": "t.db", "sql": "CREATE TABLE x (a INTEGER)"})
        outcome = Engine().run(graph)
        self.assertEqual(outcome.report[node.node_id]["status"], ERROR)

        _, node = feed(graph, "sqlite.query", {}, {
            "db": "t.db", "sql": "SELECT 1; DROP TABLE t", "allow_write": True})
        outcome = Engine().run(graph)
        self.assertEqual(outcome.report[node.node_id]["status"], ERROR)

        _, node = feed(graph, "sqlite.query", {}, {
            "db": "t.db", "sql": "PRAGMA table_info(t)"})
        outcome = Engine().run(graph)
        self.assertEqual(outcome.report[node.node_id]["status"], ERROR)

    def test_http_get_blocks_local_targets(self):
        graph = Graph()
        node = graph.add_node("http.get")
        node.set_param("url", "http://127.0.0.1:9/x")
        outcome = Engine().run(graph)
        self.assertEqual(outcome.report[node.node_id]["status"], ERROR)
        self.assertIn("запрещ", outcome.report[node.node_id]["error"])

    def test_mail_rejects_header_injection(self):
        graph = Graph()
        _, node = feed(graph, "action.mail", {"name": "Анна"}, {
            "to": "a@example.ru", "subject": "Тема\r\nBcc: evil@example.com",
            "smtp_host": "smtp.example.ru"})
        outcome = Engine().run(graph)
        self.assertEqual(outcome.report[node.node_id]["status"], ERROR)


class EngineFeatureTests(unittest.TestCase):
    def test_timeout_marks_run_as_timed_out(self):
        graph = Graph()
        src, wait = chain(graph, "logic.wait", {"seconds": 2})
        result = Engine().run(graph, {src.node_id: {"a": 1}},
                              RunOptions(timeout_s=1))
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)

    def test_cancel_stops_the_run(self):
        graph = Graph()
        src = graph.add_node("trigger.manual")
        first = graph.add_node("action.log")
        second = graph.add_node("action.log")
        graph.add_edge(src.node_id, "out", first.node_id, "in")
        graph.add_edge(first.node_id, "ok", second.node_id, "in")
        event = threading.Event()
        result = Engine().run(graph, {src.node_id: {"a": 1}},
                              RunOptions(), progress=lambda payload: event.set(),
                              cancel=event)
        self.assertTrue(result.cancelled)

    def test_retries_repeat_the_node(self):
        graph = Graph()
        src, node = chain(graph, "action.log")
        calls = []

        def boom(node_obj, inputs, context):
            calls.append(node_obj.node_id)
            raise NodeError("сломалось")

        with mock.patch("flowza.core.engine.run_node", side_effect=boom):
            result = Engine().run(graph, {src.node_id: {"a": 1}},
                                  RunOptions(retries=2))
        self.assertEqual(len(calls), 3)
        self.assertEqual(result.report[src.node_id]["attempts"], 3)
        self.assertEqual(result.report[src.node_id]["status"], ERROR)
        self.assertEqual(result.report[node.node_id]["status"], SKIPPED)

    def test_on_error_stop_skips_later_nodes(self):
        graph = Graph()
        bad = graph.add_node("source.input")
        bad.set_param("json", "{не json}")
        log1 = graph.add_node("action.log")
        graph.add_edge(bad.node_id, "out", log1.node_id, "in")
        good = graph.add_node("trigger.manual")
        log2 = graph.add_node("action.log")
        graph.add_edge(good.node_id, "out", log2.node_id, "in")
        result = Engine().run(graph, options=RunOptions(on_error="stop"))
        self.assertEqual(result.report[bad.node_id]["status"], ERROR)
        self.assertEqual(result.report[log2.node_id]["status"], SKIPPED)
        self.assertIn("остановлен", result.error)
        self.assertFalse(result.ok)

    def test_progress_reports_running_nodes(self):
        graph = Graph()
        src, node = chain(graph, "action.log")
        events = []
        Engine().run(graph, {src.node_id: {"a": 1}}, progress=events.append)
        statuses = [event.get("status") for event in events]
        self.assertIn("running", statuses)
        self.assertEqual(events[-1].get("status"), "done")

    def test_topological_order_reports_cycles(self):
        graph = Graph()
        first = graph.add_node("logic.condition")
        second = graph.add_node("logic.condition")
        graph.add_edge(first.node_id, "true", second.node_id, "in")
        graph.add_edge(second.node_id, "false", first.node_id, "in")
        order, cycles = topological_order(graph)
        self.assertEqual(order, [])
        self.assertEqual(set(cycles), {first.node_id, second.node_id})


class SchedulerTests(unittest.TestCase):
    def test_interval_clamped_and_due(self):
        graph = Graph()
        node = graph.add_node("trigger.schedule")
        node.set_param("interval", 0)
        fired = []
        now = [1000.0]
        scheduler = Scheduler(lambda: graph, lambda nid, interval: fired.append(nid),
                              clock=lambda: now[0])
        scheduler.prime()
        self.assertEqual(scheduler.due(), [])
        now[0] += 61
        self.assertEqual(scheduler.due(), [(node.node_id, 1)])
        now[0] += 61
        self.assertEqual(len(scheduler.tick_once()), 1)
        self.assertEqual(fired, [node.node_id])

    def test_no_schedule_nodes_means_nothing_due(self):
        graph = Graph()
        graph.add_node("trigger.manual")
        scheduler = Scheduler(lambda: graph, lambda *args: None,
                              clock=lambda: 0.0)
        scheduler.prime()
        self.assertEqual(scheduler.tick_once(), [])


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.graph_file = str(Path(cls.tmp.name) / "graph.json")
        os.environ["FLOWZA_GRAPH"] = cls.graph_file
        from flowza.webapp import server
        cls.server = server
        cls.client = server.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def wait_run(self, run_id, timeout=5.0):
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            body = self.client.get("/api/run/" + run_id).get_json()
            if body.get("status") != "running":
                return body
            time.sleep(0.02)
        self.fail("прогон не завершился")

    def demo_graph(self):
        graph = Graph()
        trigger = graph.add_node("trigger.manual")
        debug = graph.add_node("output.debug")
        graph.add_edge(trigger.node_id, "out", debug.node_id, "in")
        return graph.to_dict()

    def test_health_reports_version(self):
        body = self.client.get("/api/health").get_json()
        self.assertEqual(body["version"], self.server.APP_VERSION)

    def test_settings_roundtrip(self):
        response = self.client.post("/api/settings", json={"ui": {"theme": "light"}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["settings"]["ui"]["theme"], "light")
        body = self.client.get("/api/settings").get_json()["settings"]
        self.assertEqual(body["ui"]["theme"], "light")
        self.assertIn("workspace_resolved", body["files"])

    def test_settings_rejects_non_object(self):
        response = self.client.post("/api/settings", json=[1, 2])
        self.assertEqual(response.status_code, 400)

    def test_save_creates_graph_file(self):
        response = self.client.post("/api/save", json=self.demo_graph())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Path(self.graph_file).exists())

    def test_async_run_and_history(self):
        response = self.client.post("/api/run", json=self.demo_graph())
        self.assertEqual(response.status_code, 202)
        run_id = response.get_json()["run_id"]
        body = self.wait_run(run_id)
        self.assertEqual(body["status"], "done")
        self.assertIn("result", body)
        runs = self.client.get("/api/runs").get_json()["runs"]
        self.assertTrue(any(item["run_id"] == run_id for item in runs))

    def test_sync_run(self):
        response = self.client.post("/api/run?wait=1", json=self.demo_graph())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

    def test_foreign_origin_is_rejected(self):
        response = self.client.post("/api/run", json=self.demo_graph(),
                                    headers={"Origin": "http://evil.example.com"})
        self.assertEqual(response.status_code, 403)

    def test_static_traversal_blocked(self):
        for path in ("/static/../server.py", "/static/..%2fserver.py"):
            response = self.client.get(path)
            self.assertIn(response.status_code, (400, 403, 404), path)

    def test_security_headers_present(self):
        response = self.client.get("/")
        self.assertIn("default-src", response.headers.get("Content-Security-Policy", ""))
        self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")

    def test_webhook_requires_token_and_runs_graph(self):
        graph = Graph()
        node = graph.add_node("trigger.webhook")
        node.set_param("path", "test")
        debug = graph.add_node("output.debug")
        graph.add_edge(node.node_id, "out", debug.node_id, "in")
        self.client.post("/api/graph", json=graph.to_dict())

        self.server.SETTINGS.update({"webhook": {"enabled": False}})
        disabled = self.client.post("/hook/test", json={"a": 1})
        self.assertEqual(disabled.status_code, 404)

        self.server.SETTINGS.update({"webhook": {"enabled": True}})
        with mock.patch.dict(os.environ, {"FLOWZA_WEBHOOK_TOKEN": "hook-secret"}):
            bad = self.client.post("/hook/test", json={"a": 1},
                                   headers={"X-Flowza-Token": "wrong"})
            self.assertEqual(bad.status_code, 401)
            good = self.client.post("/hook/test", json={"a": 1},
                                    headers={"X-Flowza-Token": "hook-secret"})
            self.assertEqual(good.status_code, 202)
            self.wait_run(good.get_json()["run_id"])

            unknown = self.client.post("/hook/nope", json={},
                                       headers={"X-Flowza-Token": "hook-secret"})
            self.assertEqual(unknown.status_code, 404)
        self.server.SETTINGS.update({"webhook": {"enabled": False}})


if __name__ == "__main__":
    unittest.main()
