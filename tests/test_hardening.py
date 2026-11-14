"""Тесты укрепления: SSRF, ReDoS, очередь прогонов, токен API, CSV."""

import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from flowza import security
from flowza.core import engine as engine_module
from flowza.core.context import RunAborted, RunContext
from flowza.core.engine import ERROR, Engine, prepare_demo
from flowza.model import graph as graph_module
from flowza.model.graph import Graph
from flowza.model.registry import NodeError, run_node


class _Handler(BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):
        _Handler.hits.append(self.path)
        if self.path == "/redir":
            self.send_response(302)
            self.send_header("Location", "file:///etc/passwd")
            self.end_headers()
            return
        body = b'{"ok": true, "value": 42}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _Server:
    def __enter__(self):
        _Handler.hits = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class SsrfTests(unittest.TestCase):
    def test_resolve_target_returns_pinned_address(self):
        scheme, host, port, address, target = security.resolve_target(
            "http://127.0.0.1:8080/x?y=1", allow_private=True)
        self.assertEqual((scheme, host, port, target),
                         ("http", "127.0.0.1", 8080, "/x?y=1"))
        self.assertEqual(address, "127.0.0.1")

    def test_dns_rebinding_resolves_only_once(self):
        calls = []

        def fake_getaddrinfo(host, port, *args, **kwargs):
            calls.append(host)
            address = "192.0.2.1" if len(calls) == 1 else "127.0.0.1"
            return [(2, 1, 6, "", (address, port))]

        with mock.patch.object(security.socket, "getaddrinfo", fake_getaddrinfo):
            with self.assertRaises((security.SecurityError, OSError)):
                security.http_request("http://rebind.example:9/", timeout=1)
        self.assertEqual(len(calls), 1)

    def test_private_target_allowed_only_on_request(self):
        with _Server() as server:
            url = f"http://127.0.0.1:{server.port}/data"
            status, headers, text = security.http_request(
                url, allow_private=True, timeout=5)
            self.assertEqual(status, 200)
            self.assertIn("application/json", headers["Content-Type"])
            self.assertEqual(json.loads(text)["value"], 42)
            with self.assertRaises(security.SecurityError):
                security.http_request(url, timeout=5)

    def test_redirect_to_file_scheme_is_blocked(self):
        with _Server() as server:
            with self.assertRaises(security.SecurityError):
                security.http_request(f"http://127.0.0.1:{server.port}/redir",
                                      allow_private=True, timeout=5)

    def test_cancel_interrupts_request(self):
        with _Server() as server:
            event = threading.Event()
            event.set()
            context = RunContext(cancel=event)
            with self.assertRaises(RunAborted):
                security.http_request(f"http://127.0.0.1:{server.port}/data",
                                      allow_private=True, timeout=5,
                                      context=context)

    def test_http_node_reports_network_error(self):
        graph = Graph()
        node = graph.add_node("http.get")
        node.set_param("url", "http://127.0.0.1:9/none")
        outcome = Engine().run(graph)
        self.assertEqual(outcome.report[node.node_id]["status"], ERROR)


class RegexTimeoutTests(unittest.TestCase):
    def test_catastrophic_pattern_is_stopped_by_timeout(self):
        graph = Graph()
        source = graph.add_node("source.input")
        source.set_param("json", json.dumps({"text": "a" * 40 + "!"}))
        regex = graph.add_node("data.regex")
        regex.set_param("field", "text")
        regex.set_param("pattern", "(a|aa)+$")
        regex.set_param("mode", "извлечь")
        graph.add_edge(source.node_id, "out", regex.node_id, "in")
        started = time.monotonic()
        outcome = Engine().run(graph)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(outcome.report[regex.node_id]["status"], ERROR)
        message = outcome.report[regex.node_id]["error"]
        self.assertTrue("уложилось" in message or "таймаут" in message, message)

    def test_normal_pattern_still_works(self):
        graph = Graph()
        source = graph.add_node("source.input")
        source.set_param("json", json.dumps({"text": "abc123"}))
        regex = graph.add_node("data.regex")
        regex.set_param("field", "text")
        regex.set_param("pattern", r"\d+")
        regex.set_param("out_field", "match")
        graph.add_edge(source.node_id, "out", regex.node_id, "in")
        outcome = Engine().run(graph)
        self.assertEqual(outcome.results[regex.node_id]["out"]["match"], "123")


class CsvTests(unittest.TestCase):
    def test_numbers_are_not_broken(self):
        self.assertEqual(security.csv_cell("-3"), "-3")
        self.assertEqual(security.csv_cell("-1.5"), "-1.5")
        self.assertEqual(security.csv_cell("+7 999"), "'+7 999")

    def test_formulas_are_escaped(self):
        self.assertEqual(security.csv_cell("=1+1"), "'=1+1")
        self.assertEqual(security.csv_cell("@cmd"), "'@cmd")
        self.assertEqual(security.csv_cell("-3;=cmd"), "'-3;=cmd")


class DedupTests(unittest.TestCase):
    def test_single_topological_implementation(self):
        self.assertIs(engine_module.topological_order,
                      graph_module.topological_order)
        graph = Graph()
        first = graph.add_node("logic.condition")
        second = graph.add_node("logic.condition")
        graph.add_edge(first.node_id, "true", second.node_id, "in")
        graph.add_edge(second.node_id, "false", first.node_id, "in")
        self.assertEqual(graph_module.find_cycles(graph),
                         [n for n in (first.node_id, second.node_id)])


class RunQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from flowza.webapp import server
        cls.server = server
        cls.client = server.app.test_client()

    def slow_graph(self):
        graph = Graph()
        trigger = graph.add_node("trigger.manual")
        wait = graph.add_node("logic.wait")
        wait.set_param("seconds", 1)
        debug = graph.add_node("output.debug")
        graph.add_edge(trigger.node_id, "out", wait.node_id, "in")
        graph.add_edge(wait.node_id, "out", debug.node_id, "in")
        return graph.to_dict()

    def wait_finished(self, run_id, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            body = self.client.get("/api/run/" + run_id).get_json()
            if body["status"] not in ("queued", "running"):
                return body
            time.sleep(0.05)
        self.fail("прогон не завершился")

    def test_second_run_waits_in_queue(self):
        graph = self.slow_graph()
        first = self.client.post("/api/run", json=graph).get_json()
        second = self.client.post("/api/run", json=graph).get_json()
        self.assertIn(first["status"], ("running", "queued"))
        self.assertEqual(second["status"], "queued")
        started = time.monotonic()
        first_body = self.wait_finished(first["run_id"])
        second_body = self.wait_finished(second["run_id"])
        self.assertEqual(first_body["status"], "done")
        self.assertEqual(second_body["status"], "done")
        self.assertGreaterEqual(time.monotonic() - started, 0.5)

    def test_queued_run_can_be_cancelled(self):
        graph = self.slow_graph()
        active = self.client.post("/api/run", json=graph).get_json()
        queued = self.client.post("/api/run", json=graph).get_json()
        self.assertEqual(queued["status"], "queued")
        response = self.client.post("/api/cancel", json={"run_id": queued["run_id"]})
        self.assertEqual(response.status_code, 200)
        body = self.client.get("/api/run/" + queued["run_id"]).get_json()
        self.assertEqual(body["status"], "cancelled")
        self.client.post("/api/cancel", json={"run_id": active["run_id"]})
        self.wait_finished(active["run_id"])


class ApiTokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from flowza.webapp import server
        cls.client = server.app.test_client()

    def test_remote_request_needs_token(self):
        with mock.patch.dict(os.environ, {"FLOWZA_API_TOKEN": "secret-token"}):
            remote = {"REMOTE_ADDR": "192.168.1.50"}
            denied = self.client.get("/api/health", environ_base=remote)
            self.assertEqual(denied.status_code, 401)
            allowed = self.client.get("/api/health", environ_base=remote,
                                      headers={"X-Flowza-Token": "secret-token"})
            self.assertEqual(allowed.status_code, 200)
            local = self.client.get("/api/health",
                                    environ_base={"REMOTE_ADDR": "127.0.0.1"})
            self.assertEqual(local.status_code, 200)

    def test_public_host_requires_token(self):
        import main
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOWZA_API_TOKEN", None)
            self.assertEqual(main.main(["--host", "0.0.0.0", "--port", "8898"]), 2)


if __name__ == "__main__":
    unittest.main()
