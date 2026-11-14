"""Фоновый планировщик: запускает граф по нодам trigger.schedule."""

import logging
import threading
import time

log = logging.getLogger("flowza")

MIN_INTERVAL_MIN = 1
MAX_INTERVAL_MIN = 1440


class Scheduler:
    """Раз в tick секунд проверяет, какие расписания пора запускать."""

    def __init__(self, graph_provider, runner, clock=time.monotonic, tick=1.0):
        self.graph_provider = graph_provider
        self.runner = runner
        self.clock = clock
        self.tick = max(0.05, float(tick))
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last = {}
        self.runs = 0
        self.skipped = 0

    def _interval_of(self, node):
        raw = node.params.get("interval", 60)
        try:
            interval = int(float(raw))
        except (TypeError, ValueError):
            interval = 60
        return max(MIN_INTERVAL_MIN, min(MAX_INTERVAL_MIN, interval))

    def nodes(self):
        graph = self.graph_provider()
        if graph is None:
            return {}
        return {node.node_id: node for node in graph.nodes.values()
                if node.node_type == "trigger.schedule"}

    def prime(self, now=None):
        """Считать текущий момент точкой отсчёта: первый запуск — через интервал."""
        now = self.clock() if now is None else now
        with self._lock:
            self._last = {node_id: now for node_id in self.nodes()}

    def due(self, now=None):
        """Ноды, которым пора сработать; отметка времени ставится сразу."""
        now = self.clock() if now is None else now
        found = []
        current = self.nodes()
        with self._lock:
            for node_id, node in current.items():
                interval = self._interval_of(node)
                last = self._last.get(node_id)
                if last is not None and now - last < interval * 60:
                    continue
                self._last[node_id] = now
                found.append((node_id, interval))
            self._last = {node_id: stamp for node_id, stamp in self._last.items()
                          if node_id in current}
        return found

    def tick_once(self, now=None):
        started = self.due(now)
        for node_id, interval in started:
            self.runs += 1
            try:
                accepted = self.runner(node_id, interval)
            except Exception:
                log.exception("Планировщик: не удалось запустить %s", node_id)
                continue
            if accepted is False:
                self.skipped += 1
        return started

    def start(self, prime=True):
        if self._thread and self._thread.is_alive():
            return False
        if prime:
            self.prime()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="flowza-scheduler",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(2.0)
        self._thread = None

    def _loop(self):
        while not self._stop.wait(self.tick):
            try:
                self.tick_once()
            except Exception:
                log.exception("Планировщик упал в цикле")
