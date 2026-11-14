"""Проверка запущенной Flowza по HTTP (Linux-сборка, smoke-тест).

Использование: python3 packaging/verify_linux.py [http://127.0.0.1:8796]
"""

import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8796"

GRAPH = {
    "nodes": [
        {"id": "a", "type": "trigger.manual", "x": 100, "y": 100, "params": {}},
        {"id": "b", "type": "output.debug", "x": 300, "y": 100, "params": {}},
    ],
    "edges": [
        {"id": "e1", "source": "a", "source_port": "out",
         "target": "b", "target_port": "in"},
    ],
}


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        return json.load(response)


def post(path, payload):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def main():
    health = get("/api/health")
    print("health:", health["version"])
    settings = get("/api/settings")["settings"]
    print("graph_file:", settings["graph_file"])
    print("workspace:", settings["files"]["workspace_resolved"])
    run = post("/api/run?wait=1", GRAPH)
    print("run:", run["status"], run["summary"])
    print("save:", post("/api/save", GRAPH)["ok"])
    if not run.get("ok"):
        raise SystemExit("прогон завершился с ошибкой")


if __name__ == "__main__":
    main()
