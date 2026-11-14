"""Flowza — визуальный конструктор автоматизаций без кода.

Точка входа: запускает локальный web-редактор (Flask) в браузере.

Зависимости: Flask (бэкенд). Фронтенд — чистый HTML/CSS/JS, без сборки.
"""

import argparse
import os
import socket
import sys

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="flowza",
        description="Локальный визуальный конструктор автоматизаций.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help="адрес прослушивания (по умолчанию 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="порт (по умолчанию 8765)")
    parser.add_argument("--no-browser", action="store_true",
                        help="не открывать браузер автоматически")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("порт должен быть в диапазоне 1-65535")
    return args


def port_is_free(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def main(argv=None):
    args = parse_args(argv)

    from flowza.security import is_loopback_host

    if not is_loopback_host(args.host) and not os.environ.get("FLOWZA_API_TOKEN"):
        print(f"[Flowza] Адрес {args.host} открывает API всей сети.", file=sys.stderr)
        print("[Flowza] Задайте токен и передавайте его в заголовке "
              "X-Flowza-Token:", file=sys.stderr)
        print("[Flowza]   set FLOWZA_API_TOKEN=длинная-строка   "
              "(Linux: export FLOWZA_API_TOKEN=...)", file=sys.stderr)
        return 2

    try:
        from flowza.webapp.server import launch
    except ModuleNotFoundError as exc:
        missing = exc.name or "flask"
        print(f"[Flowza] Не хватает зависимости: {missing}", file=sys.stderr)
        print("[Flowza] Установите её: pip install -r requirements.txt",
              file=sys.stderr)
        return 2

    if not port_is_free(args.host, args.port):
        print(f"[Flowza] Порт {args.port} уже занят.", file=sys.stderr)
        print("[Flowza] Закройте другой экземпляр или укажите --port 8766",
              file=sys.stderr)
        return 1

    try:
        launch(host=args.host, port=args.port,
               open_browser=not args.no_browser)
    except OSError as exc:
        print(f"[Flowza] Не удалось запустить сервер: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[Flowza] Остановлено.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
