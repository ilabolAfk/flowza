"""Реестр типов нод (справочник). Каждая нода = самоописываемый коннектор.

Структура описания сообщает движку и редактору:
- icon         — ключ иконки (набор встроен во фронтенд, без внешних шрифтов)
- color        — цвет блока в канвасе
- category     — группа в палитре
- inputs       — входные порты
- outputs      — выходные порты
- params       — настройки панели (форма)

Ноды-действия выполняют реальную работу: «Вебхук» делает HTTP-запрос,
«Письмо» отправляет почту через SMTP. «Запрос к ИИ» — честная заглушка.
"""

import ast
import json
import logging
import os
import re
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from email.message import EmailMessage

log = logging.getLogger("flowza")

SMTP_TIMEOUT = 20
WAIT_LIMIT = 300
HTTP_TIMEOUT_LIMIT = 120


class NodeError(Exception):
    """Ошибка выполнения ноды с понятным пользователю текстом."""


# ------------------------------------------------------------------ параметры
S_TEXT = lambda label, key, default="", hint=None: {  # noqa: E731
    "name": key, "label": label, "type": "text", "default": default, "hint": hint}
S_INT = lambda label, key, default=0, hint=None: {  # noqa: E731
    "name": key, "label": label, "type": "int", "default": default, "hint": hint}
S_BOOL = lambda label, key, default=False, hint=None: {  # noqa: E731
    "name": key, "label": label, "type": "bool", "default": default, "hint": hint}
S_SELECT = lambda label, key, options, default=None, hint=None: {  # noqa: E731
    "name": key, "label": label, "type": "select", "options": options,
    "default": default if default is not None else options[0], "hint": hint}
S_TEXTAREA = lambda label, key, default="", hint=None: {  # noqa: E731
    "name": key, "label": label, "type": "textarea", "default": default, "hint": hint}

FILTER_OPS = ["==", "!=", ">", ">=", "<", "<=", "содержит", "пусто", "не пусто"]
CONDITION_OPS = ["<", "<=", ">", ">=", "==", "!="]


# ------------------------------------------------------------------ спецификации
NODE_SPECS = {
    # ---------------- Источники ----------------
    "trigger.manual": {
        "title": "Ручной запуск",
        "icon": "pointer", "color": "#4a90d9", "category": "Источники",
        "outputs": [{"name": "out", "kind": "data", "label": "Запуск"}],
        "params": [],
    },
    "trigger.schedule": {
        "title": "Расписание",
        "icon": "clock", "color": "#4a90d9", "category": "Источники",
        "outputs": [{"name": "out", "kind": "data", "label": "Сработало"}],
        "params": [S_INT("Интервал (мин)", "interval", 60,
                         "описание сценария: фоновый планировщик не запускается")],
    },
    "trigger.webhook": {
        "title": "Вебхук (вход)",
        "icon": "link", "color": "#4a90d9", "category": "Источники",
        "outputs": [{"name": "out", "kind": "data", "label": "Запрос"}],
        "params": [S_TEXT("URL (подпись)", "url", "/hook/flow",
                          "адрес для внешних систем; приём запросов не включён")],
    },
    "source.input": {
        "title": "Ввод данных",
        "icon": "keyboard", "color": "#4a90d9", "category": "Источники",
        "outputs": [{"name": "out", "kind": "data", "label": "Данные"}],
        "params": [S_TEXTAREA("JSON (тест)", "json",
                              '{"name":"Мария","status":"новая","balance":-3}')],
    },

    # ---------------- Логика ----------------
    "logic.condition": {
        "title": "Условие",
        "icon": "branch", "color": "#e0a63c", "category": "Логика",
        "inputs": [{"name": "in", "kind": "data", "label": "Данные"}],
        "outputs": [
            {"name": "true", "kind": "data", "label": "Да"},
            {"name": "false", "kind": "data", "label": "Нет"},
        ],
        "params": [
            S_TEXT("Поле", "field", "balance"),
            S_SELECT("Операция", "op", CONDITION_OPS, "<"),
            S_TEXT("Значение", "value", "0"),
        ],
    },
    "data.filter": {
        "title": "Фильтр",
        "icon": "filter", "color": "#e0a63c", "category": "Логика",
        "inputs": [{"name": "in", "kind": "data", "label": "Данные"}],
        "outputs": [
            {"name": "pass", "kind": "data", "label": "Прошло"},
            {"name": "drop", "kind": "data", "label": "Отсеяно"},
        ],
        "params": [
            S_TEXT("Поле", "field", "status"),
            S_SELECT("Операция", "op", FILTER_OPS, "=="),
            S_TEXT("Значение", "value", "новая"),
        ],
    },
    "logic.merge": {
        "title": "Объединить",
        "icon": "sitemap", "color": "#e0a63c", "category": "Логика",
        "inputs": [
            {"name": "a", "kind": "data", "label": "A"},
            {"name": "b", "kind": "data", "label": "B"},
        ],
        "outputs": [{"name": "out", "kind": "data", "label": "Результат"}],
        "params": [S_SELECT("Режим", "mode", ["объединить", "слить списки"],
                            "объединить")],
    },
    "logic.wait": {
        "title": "Задержка",
        "icon": "stopwatch", "color": "#e0a63c", "category": "Логика",
        "inputs": [{"name": "in", "kind": "data", "label": "Данные"}],
        "outputs": [{"name": "out", "kind": "data", "label": "Позже"}],
        "params": [S_INT("Секунд", "seconds", 1, f"0–{WAIT_LIMIT}, блокирует прогон")],
    },

    # ---------------- Данные ----------------
    "data.map": {
        "title": "Карта полей",
        "icon": "exchange", "color": "#6bbf8a", "category": "Данные",
        "inputs": [{"name": "in", "kind": "data", "label": "Данные"}],
        "outputs": [{"name": "out", "kind": "data", "label": "Результат"}],
        "params": [S_TEXT("Формула", "formula", "price * qty",
                          "имена полей данных, числа, + - * / ( )")],
    },
    "data.rename": {
        "title": "Переименовать поле",
        "icon": "rename", "color": "#6bbf8a", "category": "Данные",
        "inputs": [{"name": "in", "kind": "data", "label": "Данные"}],
        "outputs": [{"name": "out", "kind": "data", "label": "Данные"}],
        "params": [
            S_TEXT("Старое имя", "from", "name"),
            S_TEXT("Новое имя", "to", "full_name"),
        ],
    },
    "data.concat": {
        "title": "Склеить строки",
        "icon": "align", "color": "#6bbf8a", "category": "Данные",
        "inputs": [{"name": "in", "kind": "data", "label": "Данные"}],
        "outputs": [{"name": "out", "kind": "data", "label": "Строка"}],
        "params": [
            S_TEXT("Поля (через запятую)", "fields", "first,last"),
            S_TEXT("Разделитель", "sep", " "),
        ],
    },
    "data.calculate": {
        "title": "Вычислить поле",
        "icon": "calculator", "color": "#6bbf8a", "category": "Данные",
        "inputs": [{"name": "in", "kind": "data", "label": "Данные"}],
        "outputs": [{"name": "out", "kind": "data", "label": "Данные"}],
        "params": [
            S_TEXT("Новое поле", "out_field", "total"),
            S_TEXT("Выражение", "expr", "price * qty",
                   "результат пишется в поле, остальные поля сохраняются"),
        ],
    },

    # ---------------- Действия ----------------
    "action.mail": {
        "title": "Отправить письмо",
        "icon": "envelope", "color": "#d95a9b", "category": "Действия",
        "inputs": [{"name": "in", "kind": "data", "label": "Данные"}],
        "outputs": [{"name": "ok", "kind": "data", "label": "Готово"}],
        "params": [
            S_TEXT("Кому", "to", "admin@example.ru", "несколько адресов — через ;"),
            S_TEXT("Копия", "cc", "", "необязательно, через ;"),
            S_TEXT("Тема", "subject", "Уведомление: {{name}}"),
            S_TEXTAREA("Текст", "body", "Привет! Новое событие: {{name}}"),
            S_TEXT("SMTP-сервер", "smtp_host", "",
                   "пусто — взять из FLOWZA_SMTP_HOST"),
            S_INT("Порт", "smtp_port", 587, "465 — SSL, 587 — STARTTLS"),
            S_TEXT("Пользователь", "smtp_user", "",
                   "пусто — взять из FLOWZA_SMTP_USER"),
            S_TEXT("От кого", "from_addr", "", "по умолчанию — пользователь"),
            S_TEXT("Переменная с паролем", "password_env", "FLOWZA_SMTP_PASSWORD",
                   "пароль в графе не хранится"),
            S_BOOL("STARTTLS", "tls", True),
        ],
        "note": "Пароль берётся из переменной окружения, в файл графа не пишется.",
    },
    "action.ai": {
        "title": "Запрос к ИИ (заглушка)",
        "icon": "robot", "color": "#9a6bdb", "category": "Действия",
        "inputs": [{"name": "in", "kind": "data", "label": "Данные"}],
        "outputs": [{"name": "out", "kind": "data", "label": "Ответ"}],
        "params": [
            S_TEXT("Промпт", "prompt", "Кратко опиши: {{name}} — {{status}}"),
            S_SELECT("Модель", "model", ["giga", "other"], "giga"),
        ],
        "note": "Заглушка: модель не вызывается, результат помечен stub.",
    },
    "action.webhook": {
        "title": "Отослать на вебхук",
        "icon": "send", "color": "#d95a9b", "category": "Действия",
        "inputs": [{"name": "in", "kind": "data", "label": "Данные"}],
        "outputs": [{"name": "ok", "kind": "data", "label": "Готово"}],
        "params": [
            S_TEXT("URL", "url", "https://example.com/hook"),
            S_SELECT("Метод", "method", ["POST", "PUT", "GET"], "POST"),
            S_INT("Таймаут, с", "timeout", 10, f"1–{HTTP_TIMEOUT_LIMIT}"),
        ],
        "note": "Тело запроса — JSON с данными, пришедшими на вход ноды.",
    },
    "action.log": {
        "title": "Записать в журнал",
        "icon": "note", "color": "#d95a9b", "category": "Действия",
        "inputs": [{"name": "in", "kind": "data", "label": "Данные"}],
        "outputs": [{"name": "ok", "kind": "data", "label": "Готово"}],
        "params": [S_TEXT("Метка", "label", "событие")],
    },

    # ---------------- Вывод ----------------
    "output.debug": {
        "title": "Просмотр результата",
        "icon": "eye", "color": "#5b6b8a", "category": "Вывод",
        "inputs": [{"name": "in", "kind": "data", "label": "Данные"}],
        "outputs": [],
        "params": [],
    },
}


# ------------------------------------------------------------------ утилиты
def _as_number(value):
    """Привести значение к float, если это осмысленное число."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _norm_text(value):
    return str(value).strip().lower()


def _tidy(number):
    """30.0 -> 30, но 30.5 остаётся дробным."""
    if isinstance(number, float) and number.is_integer():
        return int(number)
    return number


def compare(left, op, right):
    """Сравнение с приведением типов: числа числами, строки без регистра."""
    op = (op or "==").strip().lower()
    if op in ("пусто",):
        return left is None or left == "" or left == [] or left == {}
    if op in ("не пусто", "непусто"):
        return not compare(left, "пусто", right)
    if op in ("содержит", "contains"):
        if isinstance(left, (list, tuple, set)):
            return any(_norm_text(item) == _norm_text(right) for item in left)
        return _norm_text(right) in _norm_text("" if left is None else left)

    left_num, right_num = _as_number(left), _as_number(right)
    if op in ("==", "=", "равно"):
        if left_num is not None and right_num is not None:
            return left_num == right_num
        return _norm_text("" if left is None else left) == _norm_text(right)
    if op in ("!=", "<>", "не равно"):
        return not compare(left, "==", right)

    if left_num is None or right_num is None:
        raise NodeError(
            f"нельзя сравнить {left!r} {op} {right!r}: нужны числа "
            f"(поле «{left}» не является числом)")
    if op == "<":
        return left_num < right_num
    if op == "<=":
        return left_num <= right_num
    if op == ">":
        return left_num > right_num
    if op == ">=":
        return left_num >= right_num
    raise NodeError(f"неизвестная операция сравнения: {op}")


_ALLOWED_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}


def evaluate(expression, payload):
    """Безопасно вычислить арифметическое выражение с полями из payload."""
    expression = str(expression or "").strip()
    if not expression:
        raise NodeError("пустое выражение")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise NodeError(f"не удалось разобрать выражение «{expression}»: {exc.msg}")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            return float(node.value)
        if isinstance(node, ast.Name):
            if isinstance(payload, dict) and node.id in payload:
                value = payload[node.id]
            else:
                known = ", ".join(sorted(payload)) if isinstance(payload, dict) else "нет полей"
                raise NodeError(f"поле «{node.id}» не найдено (есть: {known})")
            number = _as_number(value)
            if number is None:
                raise NodeError(f"поле «{node.id}» = {value!r}, это не число")
            return number
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            value = ev(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right == 0:
                raise NodeError("деление на ноль")
            if isinstance(node.op, ast.Pow) and abs(right) > 64:
                raise NodeError("слишком большая степень")
            return _ALLOWED_BINOPS[type(node.op)](left, right)
        raise NodeError("поддерживаются только + - * / // % **, числа, поля и скобки")

    return _tidy(ev(tree))


def _fill_template(template, payload):
    """Подставить {{поле}} из данных в строку."""
    text = "" if template is None else str(template)
    if not isinstance(payload, dict):
        return text
    for found in set(re.findall(r"\{\{(\w+)\}\}", text)):
        text = text.replace("{{" + found + "}}", str(payload.get(found, "")))
    return text


def _int_param(params, name, default, low=None, high=None):
    raw = params.get(name, default)
    if raw in ("", None):
        value = default
    else:
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            raise NodeError(f"«{name}» должно быть числом, получено {raw!r}")
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def _payload_of(inputs):
    """Данные, пришедшие на вход ноды."""
    if isinstance(inputs, dict) and "in" in inputs:
        return inputs["in"]
    return inputs


def _require_dict(payload, what):
    if not isinstance(payload, dict):
        raise NodeError(f"{what} ждёт объект данных, а получил {type(payload).__name__}")
    return payload


def _known_fields(payload):
    return ", ".join(sorted(payload)) if isinstance(payload, dict) else "нет"


# ------------------------------------------------------------------ выполнение
def run_node(node, inputs):
    """Выполнить одну ноду. Возвращает {имя_порта: значение}. Ошибки — NodeError."""
    ty = node.node_type
    spec = NODE_SPECS.get(ty)
    if spec is None:
        raise NodeError(f"неизвестный тип ноды: {ty}")
    params = node.params
    outputs = spec.get("outputs", [])

    if ty.startswith("trigger."):
        seed = inputs.get("_seed") if isinstance(inputs, dict) else None
        if seed is None:
            seed = {"event": ty}
        return {out["name"]: seed for out in outputs}

    if ty == "source.input":
        try:
            value = json.loads(params.get("json") or "{}")
        except (TypeError, ValueError) as exc:
            raise NodeError(f"невалидный JSON: {exc}")
        return {out["name"]: value for out in outputs}

    if ty == "output.debug":
        return {}

    payload = _payload_of(inputs)

    if ty == "data.filter":
        data = _require_dict(payload, "Фильтр")
        field = params.get("field", "")
        if field not in data:
            raise NodeError(f"поле «{field}» не найдено (есть: {_known_fields(data)})")
        passed = compare(data[field], params.get("op", "=="), params.get("value", ""))
        return {"pass": data if passed else None, "drop": None if passed else data}

    if ty == "logic.condition":
        data = _payload_of(inputs)
        field = params.get("field", "")
        left = data.get(field) if isinstance(data, dict) else data
        if isinstance(data, dict) and field not in data:
            raise NodeError(f"поле «{field}» не найдено (есть: {_known_fields(data)})")
        result = compare(left, params.get("op", "=="), params.get("value", ""))
        return {"true": data if result else None, "false": None if result else data}

    if ty == "logic.merge":
        mode = params.get("mode", "объединить")
        a = inputs.get("a") if isinstance(inputs, dict) else None
        b = inputs.get("b") if isinstance(inputs, dict) else None
        if mode == "слить списки":
            left = a if isinstance(a, list) else ([] if a is None else [a])
            right = b if isinstance(b, list) else ([] if b is None else [b])
            return {"out": left + right}
        if isinstance(a, dict) and isinstance(b, dict):
            merged = dict(a)
            merged.update(b)
            return {"out": merged}
        if a is None and b is None:
            return {"out": None}
        if isinstance(a, list) and isinstance(b, list):
            return {"out": a + b}
        return {"out": [value for value in (a, b) if value is not None]}

    if ty == "logic.wait":
        seconds = _int_param(params, "seconds", 0, low=0, high=WAIT_LIMIT)
        time.sleep(seconds)
        return {out["name"]: payload for out in outputs}

    if ty == "data.map":
        return {"out": evaluate(params.get("formula", ""), payload)}

    if ty == "data.rename":
        data = _require_dict(payload, "Переименование")
        old, new = params.get("from", ""), params.get("to", "")
        if old not in data:
            raise NodeError(f"поле «{old}» не найдено (есть: {_known_fields(data)})")
        renamed = {}
        for key, value in data.items():
            renamed[new if key == old else key] = value
        return {"out": renamed}

    if ty == "data.concat":
        fields = [f.strip() for f in str(params.get("fields", "")).split(",") if f.strip()]
        if isinstance(payload, dict):
            parts = [str(payload.get(f, "")) for f in fields]
        else:
            parts = [str(payload)]
        return {"out": params.get("sep", " ").join(parts)}

    if ty == "data.calculate":
        data = _require_dict(payload, "Вычисление поля")
        field = str(params.get("out_field") or "total").strip() or "total"
        result = dict(data)
        result[field] = evaluate(params.get("expr", ""), data)
        return {"out": result}

    if ty == "action.mail":
        return _send_mail(params, payload)

    if ty == "action.webhook":
        return _post_webhook(params, payload)

    if ty == "action.ai":
        prompt = _fill_template(params.get("prompt", ""), payload)
        return {"out": {
            "stub": True,
            "model": params.get("model"),
            "prompt": prompt,
            "data": payload,
            "note": "Заглушка: запрос к модели не выполнялся",
        }}

    if ty == "action.log":
        label = params.get("label", "событие")
        log.info("%s: %s", label, json.dumps(payload, ensure_ascii=False, default=str))
        return {"ok": {"logged": True, "label": label, "payload": payload}}

    return {out["name"]: payload for out in outputs}


def _send_mail(params, payload):
    host = (params.get("smtp_host") or os.environ.get("FLOWZA_SMTP_HOST") or "").strip()
    if not host:
        raise NodeError("не задан SMTP-сервер: заполните «SMTP-сервер» "
                        "или переменную FLOWZA_SMTP_HOST")

    raw_port = params.get("smtp_port") or os.environ.get("FLOWZA_SMTP_PORT") or 587
    try:
        port = int(float(raw_port))
    except (TypeError, ValueError):
        raise NodeError(f"некорректный порт SMTP: {raw_port!r}")

    user = (params.get("smtp_user") or os.environ.get("FLOWZA_SMTP_USER") or "").strip()
    password_env = (params.get("password_env") or "FLOWZA_SMTP_PASSWORD").strip()
    password = os.environ.get(password_env, "")

    recipients = [x.strip() for x in re.split(r"[;,]", str(params.get("to") or "")) if x.strip()]
    if not recipients:
        raise NodeError("не указан получатель письма")
    copies = [x.strip() for x in re.split(r"[;,]", str(params.get("cc") or "")) if x.strip()]

    sender = (params.get("from_addr") or user or "flowza@localhost").strip()
    subject = _fill_template(params.get("subject", ""), payload)
    body = _fill_template(params.get("body", ""), payload)

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    if copies:
        message["Cc"] = ", ".join(copies)
    message["Subject"] = subject
    message.set_content(body)
    all_recipients = recipients + copies

    if user and not password:
        raise NodeError(f"нет пароля для {user}: задайте переменную окружения "
                        f"{password_env}")

    use_tls = bool(params.get("tls", True))
    context = ssl.create_default_context()
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
        with server:
            if port != 465 and use_tls:
                server.starttls(context=context)
            if user:
                server.login(user, password)
            server.send_message(message, from_addr=sender, to_addrs=all_recipients)
    except (smtplib.SMTPException, OSError) as exc:
        raise NodeError(f"SMTP {host}:{port} — {exc}")

    return {"ok": {"sent": True, "to": all_recipients, "subject": subject,
                   "server": f"{host}:{port}"}}


def _post_webhook(params, payload):
    url = str(params.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise NodeError("URL должен начинаться с http:// или https://")
    method = str(params.get("method") or "POST").upper()
    if method not in ("POST", "PUT", "GET"):
        raise NodeError(f"неподдерживаемый метод: {method}")
    timeout = _int_param(params, "timeout", 10, low=1, high=HTTP_TIMEOUT_LIMIT)

    body = None
    if method != "GET":
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method, headers={
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "Flowza/1.0",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            text = response.read(2000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise NodeError(f"HTTP {exc.code} {exc.reason} от {url}")
    except urllib.error.URLError as exc:
        raise NodeError(f"сеть недоступна: {exc.reason}")
    except OSError as exc:
        raise NodeError(f"ошибка запроса: {exc}")
    return {"ok": {"status": status, "url": url, "response": text}}


# ------------------------------------------------------------------ доступ
def node_spec(node_type):
    return NODE_SPECS.get(node_type)


def all_node_types():
    return NODE_SPECS
