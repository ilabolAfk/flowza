"""Защитные механизмы: лимиты, безопасные пути и URL, заголовки, секреты.

Модуль не зависит от Flask и модели графа, чтобы его можно было использовать
и в движке, и в нодах, и в веб-слое.
"""

import hmac
import http.client
import ipaddress
import math
import re
import socket
import ssl
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

try:
    import regex as _regex
except ImportError:
    _regex = None

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
_CSV_NUMBER = re.compile(r"^[+-]?\d{1,18}(?:[.,]\d{1,10})?$")
_REGEX_TIMEOUT = 2.0
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


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


def _resolve_address(host, port, blocker):
    """Один раз разрешить хост в IP; при blocker — запретить внутренние адреса."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SecurityError(f"не удалось разрешить адрес «{host}»: {exc}")
    if not infos:
        raise SecurityError(f"не удалось разрешить адрес «{host}»")
    chosen = None
    for info in infos:
        raw_ip = str(info[4][0]).split("%")[0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            raise SecurityError("некорректный адрес хоста")
        if blocker is not None and blocker(ip):
            raise SecurityError("адрес ведёт во внутреннюю сеть: запрос запрещён")
        if chosen is None:
            chosen = str(ip)
    return chosen


def resolve_target(raw, *, allow_private=False):
    """Разобрать URL и вернуть (scheme, host, port, ip, target).

    Хост резолвится здесь один раз; соединение потом идёт на проверенный IP,
    поэтому DNS-rebinding между проверкой и запросом не срабатывает.
    """
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
    port = port or (443 if parts.scheme == "https" else 80)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query

    if allow_private:
        return parts.scheme, host, port, _resolve_address(host, port, None), target

    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        raise SecurityError("запросы к localhost запрещены настройками")
    if host.endswith((".local", ".internal", ".home.arpa")):
        raise SecurityError("запросы во внутреннюю сеть запрещены настройками")
    address = _resolve_address(host, port, _ip_blocked)
    return parts.scheme, host, port, address, target


def safe_url(raw, *, allow_private=False):
    """Проверить URL: схема, отсутствие учётных данных, публичный адрес."""
    resolve_target(raw, allow_private=allow_private)
    return str(raw or "").strip()


SSL_CONTEXT = ssl.create_default_context()


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Соединение строго с проверенным IP; имя хоста нужно для Host."""

    def __init__(self, host, port, address, timeout):
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self):
        self.sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock.settimeout(self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS с проверенным IP: SNI и сертификат проверяются по имени хоста."""

    def __init__(self, host, port, address, timeout):
        super().__init__(host, port, timeout=timeout, context=SSL_CONTEXT)
        self._address = address

    def connect(self):
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        self.sock.settimeout(self.timeout)


def _read_limited(response, connection, deadline, max_bytes, context):
    """Читать тело кусками, проверяя отмену и общий дедлайн."""
    chunks = []
    total = 0
    while True:
        if context is not None:
            context.check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SecurityError("истёк таймаут запроса")
        if connection.sock is not None:
            try:
                connection.sock.settimeout(min(remaining, 5.0))
            except OSError:
                pass
        try:
            chunk = response.read(65536)
        except (TimeoutError, socket.timeout):
            continue
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise SecurityError("ответ сервера слишком большой")
        chunks.append(chunk)
    return b"".join(chunks)


def _pinned_request(scheme, host, port, address, target, *, method, payload,
                    headers, deadline, max_bytes, context):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SecurityError("истёк таймаут запроса")
    connection_class = (_PinnedHTTPSConnection if scheme == "https"
                        else _PinnedHTTPConnection)
    connection = connection_class(host, port, address, min(5.0, remaining))
    try:
        connection.request(method, target, body=payload, headers=headers)
        response = connection.getresponse()
        status = response.status
        out_headers = {name: value for name, value in response.getheaders()}
        if status in _REDIRECT_CODES:
            return status, out_headers, out_headers.get("Location"), b""
        raw = _read_limited(response, connection, deadline, max_bytes, context)
        return status, out_headers, None, raw
    finally:
        connection.close()


def http_request(url, *, method="GET", body=None, timeout=10, headers=None,
                 allow_private=False, max_bytes=MAX_HTTP_RESPONSE, context=None):
    """HTTP-запрос без прокси: адрес проверяется один раз, соединение идёт на IP."""
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

    method = str(method or "GET").upper()
    if method not in ("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"):
        raise SecurityError(f"неподдерживаемый метод: {method}")
    deadline = time.monotonic() + max(1.0, float(timeout))
    current = str(url or "").strip()

    for _ in range(4):
        if context is not None:
            context.check()
        scheme, host, port, address, target = resolve_target(
            current, allow_private=allow_private)
        status, out_headers, location, raw = _pinned_request(
            scheme, host, port, address, target, method=method, payload=payload,
            headers=prepared, deadline=deadline, max_bytes=max_bytes,
            context=context)
        if location and status in _REDIRECT_CODES:
            current = urljoin(current, location)
            if status == 303 or (status in (301, 302) and method not in ("GET", "HEAD")):
                method, payload = "GET", None
            continue
        charset = ""
        for name, value in out_headers.items():
            if name.lower() == "content-type" and "charset=" in value.lower():
                charset = value.lower().split("charset=", 1)[1].split(";")[0].strip()
        try:
            text = raw.decode(charset or "utf-8", "replace")
        except LookupError:
            text = raw.decode("utf-8", "replace")
        return status, out_headers, text

    raise SecurityError("слишком много перенаправлений")


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


def compile_regex(pattern, flags=0):
    """Скомпилировать выражение: эвристика + движок regex с таймаутом."""
    text = str(pattern or "")
    if not text:
        raise SecurityError("пустое регулярное выражение")
    if len(text) > MAX_REGEX_PATTERN:
        raise SecurityError(f"регулярное выражение длиннее {MAX_REGEX_PATTERN} символов")
    if _NESTED_QUANTIFIER.search(text):
        raise SecurityError("вложенные квантификаторы запрещены: шаблон может "
                            "подвесить прогон")
    module = _regex if _regex is not None else re
    try:
        return module.compile(text, flags)
    except Exception as exc:
        raise SecurityError(f"некорректное регулярное выражение: {exc}")


def _timed(pattern, operation, timeout=_REGEX_TIMEOUT):
    """Выполнить операцию regex; движок regex прерывает по таймауту."""
    if _regex is not None and isinstance(pattern, _regex.Pattern):
        try:
            return operation(pattern, timeout)
        except TimeoutError:
            raise SecurityError(
                f"регулярное выражение не уложилось в {timeout:g} с")
    return operation(pattern, None)


def regex_search(pattern, subject, timeout=_REGEX_TIMEOUT):
    return _timed(pattern, lambda rx, limit: (
        rx.search(subject, timeout=limit) if limit is not None else rx.search(subject)),
        timeout)


def regex_sub(pattern, replacement, subject, timeout=_REGEX_TIMEOUT):
    return _timed(pattern, lambda rx, limit: (
        rx.sub(replacement, subject, timeout=limit) if limit is not None
        else rx.sub(replacement, subject)), timeout)


def regex_split(pattern, subject, timeout=_REGEX_TIMEOUT):
    return _timed(pattern, lambda rx, limit: (
        rx.split(subject, timeout=limit) if limit is not None
        else rx.split(subject)), timeout)


def csv_cell(value):
    """Защита от CSV-инъекций: числа-строки не портим, формулы экранируем."""
    if isinstance(value, str) and value.startswith(_CSV_RISKY):
        if _CSV_NUMBER.match(value):
            return value
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


def is_loopback_host(host):
    """Адрес слушает только этот компьютер?"""
    text = str(host or "").strip().lower()
    if text == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def origin_host(origin):
    """Имя хоста из заголовка Origin или None, если заголовок не разобрать."""
    if not origin:
        return None
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https"):
        return None
    return (parts.hostname or "").lower() or None
