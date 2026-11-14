"""Защитные механизмы: лимиты, безопасные пути и URL, заголовки, секреты.

Модуль не зависит от Flask и модели графа, чтобы его можно было использовать
и в движке, и в нодах, и в веб-слое.
"""

import hmac
import ipaddress
import math
import re
import socket
import ssl
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

MAX_GRAPH_NODES = 300
MAX_GRAPH_EDGES = 1000
MAX_PARAM_CHARS = 20_000
MAX_STRING_CHARS = 200_000
MAX_FILE_BYTES = 5_000_000
MAX_HTTP_RESPONSE = 5_000_000
MAX_HTTP_BODY = 1_000_000
MAX_HTTP_URL = 2_048
MAX_REGEX_PATTERN = 500
MAX_REGEX_SUBJECT = 200_000
MAX_SQL_CHARS = 20_000
MAX_SQL_ROWS = 10_000
MAX_CSV_ROWS = 10_000
MAX_ARRAY_ITEMS = 5_000
MAX_JSON_DEPTH = 40
MAX_JSON_NODES = 50_000
MAX_EMAILS = 50
MAX_LOG_CHARS = 4_000
MAX_EXPRESSION_CHARS = 500
MAX_EXPRESSION_DEPTH = 40

ALLOWED_WRITE_EXT = frozenset({".json", ".csv", ".tsv", ".txt", ".md", ".log"})

_PATH_PART_LIMIT = 500
_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.I)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NESTED_QUANTIFIER = re.compile(r"\((?:\\.|[^()])*[+*]\)[+*{]", re.S)
_EMAIL = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}"
    r"@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251})[A-Za-z0-9]$"
)
_CSV_RISKY = ("=", "+", "-", "@", "\t", "\r")


class SecurityError(ValueError):
    """Попытка выйти за разрешённые рамки: путь, URL, размер, формат."""


def coerce_int(value, default=None, low=None, high=None, name="значение"):
    """Строгое приведение к int: bool, NaN и мусор отвергаются."""
    if value is None or value == "":
        if default is None:
            raise SecurityError(f"{name}: пустое число")
        number = int(default)
    elif isinstance(value, bool):
        raise SecurityError(f"{name}: ожидалось число, получено {value!r}")
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise SecurityError(f"{name}: недопустимое число")
        number = int(value)
    elif isinstance(value, str):
        text = value.strip().replace("\u00a0", "").replace(" ", "")
        try:
            number = int(float(text))
        except (TypeError, ValueError):
            raise SecurityError(f"{name}: ожидалось число, получено {value!r}")
    else:
        raise SecurityError(f"{name}: ожидалось число, получено {type(value).__name__}")
    if low is not None:
        number = max(low, number)
    if high is not None:
        number = min(high, number)
    return number


def safe_text(value, limit=MAX_STRING_CHARS, name="значение"):
    """Строка без управляющих символов, ограниченной длины."""
    text = "" if value is None else str(value)
    text = _CONTROL.sub("", text)
    if len(text) > limit:
        raise SecurityError(f"{name}: длиннее {limit} символов")
    return text


def header_text(value, limit=255, name="заголовок"):
    """Значение почтового/HTTP-заголовка: без CR, LF и управляющих символов."""
    text = "" if value is None else str(value)
    if "\r" in text or "\n" in text:
        raise SecurityError(f"{name}: перевод строки запрещён")
    text = _CONTROL.sub("", text).strip()
    if len(text) > limit:
        raise SecurityError(f"{name}: длиннее {limit} символов")
    return text


def parse_emails(raw, limit=MAX_EMAILS):
    """Разобрать список адресов через ; или , с проверкой каждого."""
    items = [chunk.strip() for chunk in re.split(r"[;,]", str(raw or "")) if chunk.strip()]
    if len(items) > limit:
        raise SecurityError(f"слишком много адресов: максимум {limit}")
    for item in items:
        if "\r" in item or "\n" in item or len(item) > 320 or not _EMAIL.match(item):
            raise SecurityError(f"некорректный адрес: {item[:60]!r}")
    return items


def safe_path(base, raw, *, for_write=False, allowed_exts=ALLOWED_WRITE_EXT,
              max_bytes=MAX_FILE_BYTES):
    """Путь внутри base: без абсолютных путей, '..', симлинков и устройств."""
    base_path = Path(base).resolve()
    text = str(raw or "").strip()
    if not text:
        raise SecurityError("пустой путь к файлу")
    if len(text) > _PATH_PART_LIMIT:
        raise SecurityError("слишком длинный путь")
    if "\x00" in text:
        raise SecurityError("недопустимый символ в пути")

    candidate = Path(text)
    if candidate.is_absolute() or candidate.drive:
        raise SecurityError("абсолютные пути запрещены: укажите файл внутри рабочей папки")
    parts = [part for part in candidate.parts if part not in ("", ".")]
    if not parts:
        raise SecurityError("пустой путь к файлу")
    for part in parts:
        if part == "..":
            raise SecurityError("выход за пределы рабочей папки запрещён")
        if part.endswith(" ") or part.endswith("."):
            raise SecurityError(f"недопустимое имя файла: {part!r}")
        if _WINDOWS_RESERVED.match(part):
            raise SecurityError(f"зарезервированное имя файла: {part!r}")

    target = (base_path / candidate).resolve()
    if target == base_path or base_path not in target.parents:
        raise SecurityError("путь ведёт за пределы рабочей папки")
    if target.exists() and target.is_symlink():
        raise SecurityError("символические ссылки запрещены")

    if for_write:
        if allowed_exts and target.suffix.lower() not in allowed_exts:
            raise SecurityError(
                "разрешены только файлы " + ", ".join(sorted(allowed_exts)))
        if target.parent.exists() and not target.parent.is_dir():
            raise SecurityError("часть пути занята файлом")
        if target.exists() and target.stat().st_size > max_bytes:
            raise SecurityError("файл слишком большой для перезаписи")
    else:
        if not target.exists():
            raise SecurityError("файл не найден")
        if not target.is_file():
            raise SecurityError("это не файл")
        if target.stat().st_size > max_bytes:
            raise SecurityError("файл слишком большой")
    return target


def _ip_blocked(ip):
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return _ip_blocked(ip.ipv4_mapped)
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified):
        return True
    if ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"):
        return True
    return False


def safe_url(raw, *, allow_private=False):
    """Проверить URL: схема, отсутствие учётных данных, публичный адрес."""
    text = str(raw or "").strip()
    if not text:
        raise SecurityError("пустой URL")
    if len(text) > MAX_HTTP_URL:
        raise SecurityError("URL слишком длинный")
    if any(char.isspace() for char in text):
        raise SecurityError("в URL недопустимы пробелы")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise SecurityError("URL должен начинаться с http:// или https://")
    if parts.username or parts.password:
        raise SecurityError("учётные данные внутри URL запрещены")
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise SecurityError("в URL не указан хост")
    try:
        port = parts.port
    except ValueError:
        raise SecurityError("некорректный порт в URL")
    if port is not None and not 1 <= port <= 65535:
        raise SecurityError("некорректный порт в URL")
    if allow_private:
        return text

    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        raise SecurityError("запросы к localhost запрещены настройками")
    if host.endswith((".local", ".internal", ".home.arpa")):
        raise SecurityError("запросы во внутреннюю сеть запрещены настройками")
    try:
        infos = socket.getaddrinfo(
            host, port or (443 if parts.scheme == "https" else 80),
            type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SecurityError(f"не удалось разрешить адрес «{host}»: {exc}")
    if not infos:
        raise SecurityError(f"не удалось разрешить адрес «{host}»")
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            raise SecurityError("некорректный адрес хоста")
        if _ip_blocked(ip):
            raise SecurityError("адрес ведёт во внутреннюю сеть: запрос запрещён")
    return text


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Редирект проверяется тем же правилом, что и исходный адрес."""

    max_redirections = 3
    max_repeats = 2

    def __init__(self, allow_private):
        self.allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url(newurl, allow_private=self.allow_private)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


SSL_CONTEXT = ssl.create_default_context()


def http_request(url, *, method="GET", body=None, timeout=10, headers=None,
                 allow_private=False, max_bytes=MAX_HTTP_RESPONSE):
    """HTTP-запрос без прокси, с проверкой адреса и ограничением ответа."""
    safe_url(url, allow_private=allow_private)
    payload = None
    if body is not None:
        if not isinstance(body, (bytes, bytearray)):
            raise SecurityError("тело запроса должно быть bytes")
        if len(body) > MAX_HTTP_BODY:
            raise SecurityError("тело запроса слишком большое")
        payload = bytes(body)
    prepared = {}
    for name, value in (headers or {}).items():
        prepared[header_text(name, limit=64)] = header_text(value, limit=2048)

    request = urllib.request.Request(url, data=payload,
                                     method=str(method or "GET").upper(),
                                     headers=prepared)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _SafeRedirect(allow_private),
        urllib.request.HTTPSHandler(context=SSL_CONTEXT),
    )
    with opener.open(request, timeout=max(1.0, float(timeout))) as response:
        raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise SecurityError("ответ сервера слишком большой")
        charset = response.headers.get_content_charset() or "utf-8"
        try:
            text = raw.decode(charset, "replace")
        except LookupError:
            text = raw.decode("utf-8", "replace")
        return response.status, dict(response.headers), text


def check_json(value, *, max_depth=MAX_JSON_DEPTH, max_nodes=MAX_JSON_NODES):
    """Ограничить глубину и объём разобранного JSON."""
    stack = [(value, 1)]
    seen = 0
    while stack:
        current, depth = stack.pop()
        seen += 1
        if seen > max_nodes:
            raise SecurityError("JSON слишком большой")
        if depth > max_depth:
            raise SecurityError("слишком глубокая вложенность JSON")
        if isinstance(current, dict):
            for key, item in current.items():
                if not isinstance(key, str) or len(key) > 200:
                    raise SecurityError("недопустимый ключ JSON")
                stack.append((item, depth + 1))
        elif isinstance(current, (list, tuple)):
            stack.append((len(current), depth))
            for item in current:
                stack.append((item, depth + 1))
        elif isinstance(current, str) and len(current) > MAX_STRING_CHARS:
            raise SecurityError("слишком длинная строка в JSON")
    return value


def compile_regex(pattern):
    """Скомпилировать регулярное выражение с защитой от катастрофического бэктрекинга."""
    text = str(pattern or "")
    if not text:
        raise SecurityError("пустое регулярное выражение")
    if len(text) > MAX_REGEX_PATTERN:
        raise SecurityError(f"регулярное выражение длиннее {MAX_REGEX_PATTERN} символов")
    if _NESTED_QUANTIFIER.search(text):
        raise SecurityError("вложенные квантификаторы запрещены: шаблон может "
                            "подвесить прогон")
    try:
        return re.compile(text)
    except re.error as exc:
        raise SecurityError(f"некорректное регулярное выражение: {exc}")


def csv_cell(value):
    """Защита от CSV-инъекций при выгрузке."""
    if isinstance(value, str) and value.startswith(_CSV_RISKY):
        return "'" + value
    return value


def token_ok(provided, expected):
    """Сравнение секретов за постоянное время."""
    if not expected:
        return False
    left = (provided or "").encode("utf-8", "replace")
    right = expected.encode("utf-8", "replace")
    return hmac.compare_digest(left, right)


def safe_log_text(value, limit=MAX_LOG_CHARS):
    text = _CONTROL.sub(" ", str(value))
    return text[:limit]


class RateLimiter:
    """Простой ограничитель частоты: не больше limit событий за window секунд."""

    def __init__(self, limit, window):
        self.limit = int(limit)
        self.window = float(window)
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, key):
        now = time.monotonic()
        with self._lock:
            bucket = [stamp for stamp in self._hits.get(key, [])
                      if now - stamp < self.window]
            if len(bucket) >= self.limit:
                self._hits[key] = bucket
                return False
            bucket.append(now)
            self._hits[key] = bucket
            if len(self._hits) > 512:
                self._hits = {name: stamps for name, stamps in self._hits.items()
                              if stamps and now - stamps[-1] < self.window}
            return True


def origin_host(origin):
    """Имя хоста из заголовка Origin или None, если заголовок не разобрать."""
    if not origin:
        return None
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https"):
        return None
    return (parts.hostname or "").lower() or None
