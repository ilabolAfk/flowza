"""Реестр типов нод (справочник). Каждая нода = самоописываемый коннектор.

Структура описания сообщает движку и редактору:
- icon         — ключ иконки (набор встроен во фронтенд, без внешних шрифтов)
- color        — цвет блока в канвасе
- category     — группа в палитре
- inputs       — входные порты
- outputs      — выходные порты
- params       — настройки панели (форма)

Все опасные операции проходят проверку в flowza.security: пути заперты в
рабочей папке, URL проверяются на внутренние адреса, размеры ограничены,
заголовки писем очищаются от переводов строк.
"""

import ast
import csv
import io
import json
import logging
import math
import os
import re
import smtplib
import sqlite3
import ssl
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from .. import settings as settings_module
from ..security import (
    ALLOWED_WRITE_EXT,
    MAX_ARRAY_ITEMS,
    MAX_CSV_ROWS,
    MAX_EXPRESSION_CHARS,
    MAX_EXPRESSION_DEPTH,
    MAX_FILE_BYTES,
    MAX_HTTP_BODY,
    MAX_HTTP_RESPONSE,
    MAX_REGEX_SUBJECT,
    MAX_SQL_CHARS,
    MAX_SQL_ROWS,
    SecurityError,
    check_json,
    coerce_int,
    compile_regex,
    csv_cell,
    header_text,
    http_request,
    parse_emails,
    safe_log_text,
    safe_path,
    safe_text,
)

log = logging.getLogger("flowza")

SMTP_TIMEOUT = 20
WAIT_LIMIT = 300
HTTP_TIMEOUT_LIMIT = 120
MAX_CELL_CHARS = 10_000


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

ENCODINGS = {
    "utf-8": "utf-8",
    "utf-8-sig": "utf-8-sig",
    "windows-1251": "cp1251",
    "cp866": "cp866",
}
TEXT_EXTENSIONS = {".json": "json", ".csv": "csv", ".tsv": "csv",
                   ".txt": "txt", ".md": "txt", ".log": "txt"}


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
                         "1–1440; работает, пока приложение запущено")],
        "note": "Срабатывает по интервалу, пока приложение запущено.",
    },
    "trigger.webhook": {
        "title": "Вебхук (вход)",
        "icon": "link", "color": "#4a90d9", "category": "Источники",
        "outputs": [{"name": "out", "kind": "data", "label": "Запрос"}],
        "params": [S_TEXT("Суффикс адреса", "path", "",
                          "пусто — идентификатор ноды; адрес: /hook/<суффикс>")],
        "note": "Принимает POST /hook/<суффикс>. Токен — в FLOWZA_WEBHOOK_TOKEN.",
    },
    "source.input": {
        "title": "Ввод данных",
        "icon": "keyboard", "color": "#4a90d9", "category": "Источники",
        "outputs": [{"name": "out", "kind": "data", "label": "Данные"}],
        "params": [S_TEXTAREA("JSON (тест)", "json",
                              '{"name":"Мария","status":"новая","balance":-3}')],
    },
    "http.get": {
        "title": "HTTP GET",
        "icon": "globe", "color": "#3d7ea6", "category": "Источники",
        "outputs": [{"name": "out", "kind": "data", "label": "Ответ"}],
        "params": [
            S_TEXT("URL", "url", "https://api.example.com/items"),
            S_INT("Таймаут, с", "timeout", 10, f"1–{HTTP_TIMEOUT_LIMIT}"),
            S_TEXT("Путь в JSON", "path", "",
                   "например items[0].name; пусто — весь ответ"),
            S_TEXTAREA("Заголовки (JSON)", "headers", "{}"),
        ],
        "note": "Внутренние адреса запрещены, пока не разрешены в настройках.",
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
    "data.foreach": {
        "title": "Цикл по элементам",
        "icon": "loop", "color": "#6bbf8a", "category": "Данные",
        "inputs": [{"name": "in", "kind": "data", "label": "Данные"}],
        "outputs": [{"name": "out", "kind": "data", "label": "Список"}],
        "params": [
            S_TEXT("Поле с массивом", "field", "items", "пусто — сам вход"),
            S_TEXT("Формула", "formula", "",
                   "считается для каждого элемента: поля элемента, item, index"),
            S_TEXT("Поле результата", "out_field", "value",
                   "куда записать результат в объекте элемента"),
        ],
        "note": "Возвращает новый массив; не более 5000 элементов.",
    },
    "data.aggregate": {
        "title": "Агрегация",
        "icon": "sigma", "color": "#6bbf8a", "category": "Данные",
        "inputs": [{"name": "in", "kind": "data", "label": "Данные"}],
        "outputs": [{"name": "out", "kind": "data", "label": "Итог"}],
        "params": [
            S_TEXT("Поле с массивом", "field", "items", "пусто — сам вход"),
            S_SELECT("Операция", "op",
                     ["сумма", "количество", "среднее", "минимум", "максимум",
                      "уникальные", "склеить"], "сумма"),
            S_TEXT("Разделитель", "sep", ", ", "для операции «склеить»"),
        ],
    },
    "data.regex": {
        "title": "Регулярное выражение",
        "icon": "code", "color": "#6bbf8a", "category": "Данные",
        "inputs": [{"name": "in", "kind": "data", "label": "Данные"}],
        "outputs": [{"name": "out", "kind": "data", "label": "Данные"}],
        "params": [
            S_TEXT("Поле", "field", "text"),
            S_TEXT("Шаблон", "pattern", r"\d+", "до 500 символов"),
            S_SELECT("Действие", "mode", ["извлечь", "заменить", "разделить"],
                     "извлечь"),
            S_INT("Группа", "group", 0, "0–9, для «извлечь»"),
            S_TEXT("Замена", "replacement", "", "для «заменить»"),
            S_SELECT("Флаги", "flags", ["нет", "без регистра", "многострочный"],
                     "нет"),
            S_TEXT("Поле результата", "out_field", "match"),
        ],
        "note": "Вложенные квантификаторы запрещены: защита от зависания.",
    },
    "data.datetime": {
        "title": "Дата и время",
        "icon": "calendar", "color": "#6bbf8a", "category": "Данные",
        "inputs": [{"name": "in", "kind": "data", "label": "Данные"}],
        "outputs": [{"name": "out", "kind": "data", "label": "Данные"}],
        "params": [
            S_TEXT("Поле", "field", "created_at"),
            S_SELECT("Действие", "action", ["разобрать", "сдвинуть", "формат"],
                     "разобрать"),
            S_TEXT("Формат", "format", "%Y-%m-%d %H:%M:%S",
                   "для разбора и вывода"),
            S_INT("Сдвиг", "amount", 0, "для «сдвинуть»"),
            S_SELECT("Единица", "unit", ["секунд", "минут", "часов", "дней"],
                     "дней"),
            S_INT("Часовой пояс", "tz_offset", 3, "-12…14"),
            S_TEXT("Поле результата", "out_field", "created_at"),
        ],
    },

    # ---------------- Файлы и БД ----------------
    "io.file.read": {
        "title": "Прочитать файл",
        "icon": "file-in", "color": "#56a3a6", "category": "Файлы и БД",
        "outputs": [{"name": "out", "kind": "data", "label": "Данные"}],
        "params": [
            S_TEXT("Файл", "path", "data.csv", "путь внутри рабочей папки"),
            S_SELECT("Формат", "format", ["авто", "json", "csv", "txt"], "авто"),
            S_SELECT("Кодировка", "encoding",
                     ["utf-8", "utf-8-sig", "windows-1251", "cp866"], "utf-8"),
        ],
        "note": "Только файлы рабочей папки, до 5 МБ, до 10000 строк.",
    },
    "io.file.write": {
        "title": "Записать файл",
        "icon": "file-out", "color": "#56a3a6", "category": "Файлы и БД",
        "inputs": [{"name": "in", "kind": "data", "label": "Данные"}],
        "outputs": [{"name": "ok", "kind": "data", "label": "Готово"}],
        "params": [
            S_TEXT("Файл", "path", "result.csv", "путь внутри рабочей папки"),
            S_SELECT("Формат", "format", ["авто", "json", "csv", "txt"], "авто"),
            S_SELECT("Кодировка", "encoding",
                     ["utf-8", "utf-8-sig", "windows-1251", "cp866"], "utf-8"),
            S_TEXT("Разделитель CSV", "delimiter", ";", "один символ"),
            S_BOOL("Дозапись", "append", False),
        ],
        "note": "Разрешены .json, .csv, .tsv, .txt, .md, .log; до 5 МБ.",
    },
    "sqlite.query": {
        "title": "Запрос к SQLite",
        "icon": "database", "color": "#56a3a6", "category": "Файлы и БД",
        "inputs": [{"name": "in", "kind": "data", "label": "Данные"}],
        "outputs": [{"name": "out", "kind": "data", "label": "Результат"}],
        "params": [
            S_TEXT("Файл БД", "db", "flowza.db", "путь внутри рабочей папки"),
            S_TEXTAREA("SQL", "sql", "SELECT * FROM items LIMIT 10"),
            S_TEXTAREA("Параметры (JSON)", "params", "[]"),
            S_BOOL("Разрешить запись", "allow_write", False),
        ],
        "note": "Только параметризованный запрос. По умолчанию база только для чтения.",
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
                   "пусто — настройки или FLOWZA_SMTP_HOST"),
            S_INT("Порт", "smtp_port", 587, "465 — SSL, 587 — STARTTLS"),
            S_TEXT("Пользователь", "smtp_user", "",
                   "пусто — настройки или FLOWZA_SMTP_USER"),
            S_TEXT("От кого", "from_addr", "", "по умолчанию — пользователь"),
            S_TEXT("Переменная с паролем", "password_env", "",
                   "пусто — настройки или FLOWZA_SMTP_PASSWORD"),
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
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise NodeError(f"выражение длиннее {MAX_EXPRESSION_CHARS} символов")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise NodeError(f"не удалось разобрать выражение «{expression}»: {exc.msg}")

    def ev(node, depth=0):
        if depth > MAX_EXPRESSION_DEPTH:
            raise NodeError("слишком сложное выражение")
        if isinstance(node, ast.Expression):
            return ev(node.body, depth)
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
            value = ev(node.operand, depth + 1)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
            left, right = ev(node.left, depth + 1), ev(node.right, depth + 1)
            if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right == 0:
                raise NodeError("деление на ноль")
            if isinstance(node.op, ast.Pow) and abs(right) > 64:
                raise NodeError("слишком большая степень")
            result = _ALLOWED_BINOPS[type(node.op)](left, right)
            if isinstance(result, float) and not math.isfinite(result):
                raise NodeError("слишком большое число")
            return result
        raise NodeError("поддерживаются только + - * / // % **, числа, поля и скобки")

    return _tidy(ev(tree))


def _fill_template(template, payload):
    """Подставить {{поле}} из данных в строку."""
    text = "" if template is None else str(template)
    if len(text) > 200_000:
        raise NodeError("шаблон слишком длинный")
    if not isinstance(payload, dict):
        return text
    for found in set(re.findall(r"\{\{(\w+)\}\}", text)):
        replacement = payload.get(found, "")
        if isinstance(replacement, (dict, list)):
            replacement = json.dumps(replacement, ensure_ascii=False, default=str)
        text = text.replace("{{" + found + "}}", str(replacement)[:MAX_CELL_CHARS])
    return text


def _int_param(params, name, default, low=None, high=None):
    try:
        return coerce_int(params.get(name, default), default, low, high, name)
    except SecurityError as exc:
        raise NodeError(str(exc))


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


def _settings():
    return settings_module.current()


def _allow_private_urls():
    return bool(_settings().get("security", {}).get("allow_private_urls"))


_PATH_TOKEN = re.compile(r"([^\[\].]+)|\[(\d+)\]")


def resolve_path(value, path):
    """Доступ к вложенному полю: items[0].name."""
    text = str(path or "").strip()
    if not text:
        return value
    if len(text) > 300:
        raise NodeError("слишком длинный путь к полю")
    current = value
    for match in _PATH_TOKEN.finditer(text):
        key, index = match.group(1), match.group(2)
        if key is not None:
            if not isinstance(current, dict) or key not in current:
                raise NodeError(f"в данных нет поля «{key}»")
            current = current[key]
        else:
            if not isinstance(current, list):
                raise NodeError("путь ожидает массив")
            position = int(index)
            if position >= len(current):
                raise NodeError(f"в массиве нет элемента {position}")
            current = current[position]
    return current


# ------------------------------------------------------------------ файлы
def _encoding(params):
    return ENCODINGS.get(str(params.get("encoding") or "utf-8"), "utf-8")


def _workspace():
    return settings_module.workspace(_settings())


def _cell(value):
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > MAX_CELL_CHARS:
        raise NodeError("ячейка длиннее 10000 символов")
    if re.fullmatch(r"-?\d{1,15}(?:[.,]\d{1,10})?", text):
        number = _as_number(text)
        if number is not None:
            return _tidy(number)
    return text


def _parse_csv(text):
    sample = text[:8192]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
    except csv.Error:
        dialect = None
    stream = io.StringIO(text)
    if dialect is not None:
        reader = csv.DictReader(stream, dialect=dialect)
    else:
        reader = csv.DictReader(stream, delimiter=delimiter)
    rows = []
    for row in reader:
        if len(rows) >= MAX_CSV_ROWS:
            raise NodeError(f"в файле больше {MAX_CSV_ROWS} строк")
        clean = {}
        for key, value in row.items():
            name = str(key or "").strip()[:200]
            clean[name] = _cell(value)
        rows.append(clean)
    return rows


def _file_read(params):
    target = safe_path(_workspace(), params.get("path"), for_write=False)
    fmt = str(params.get("format") or "авто")
    if fmt == "авто":
        fmt = TEXT_EXTENSIONS.get(target.suffix.lower(), "txt")
    raw = target.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        raise NodeError("файл больше 5 МБ")
    text = raw.decode(_encoding(params), "replace")
    if fmt == "json":
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise NodeError(f"невалидный JSON в {target.name}: {exc}")
        check_json(value)
        return {"out": value}
    if fmt == "csv":
        return {"out": _parse_csv(text)}
    return {"out": text}


def _csv_rows(payload):
    if isinstance(payload, dict):
        items = [payload]
    elif isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
        items = payload
    else:
        raise NodeError("для CSV нужен объект или список объектов")
    if len(items) > MAX_CSV_ROWS:
        raise NodeError(f"больше {MAX_CSV_ROWS} строк не сохраняется")
    rows = []
    for item in items:
        row = {}
        for key, value in item.items():
            name = str(key)[:200]
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, default=str)
            if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
                value = value[:MAX_CELL_CHARS]
            row[name] = csv_cell("" if value is None else value)
        rows.append(row)
    return rows


def _write_bytes(target, data, append):
    if len(data) > MAX_FILE_BYTES:
        raise NodeError("файл больше 5 МБ")
    target.parent.mkdir(parents=True, exist_ok=True)
    if append:
        with open(target, "ab") as handle:
            handle.write(data)
    else:
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    return len(data)


def _file_write(params, payload):
    target = safe_path(_workspace(), params.get("path"), for_write=True,
                       allowed_exts=ALLOWED_WRITE_EXT)
    fmt = str(params.get("format") or "авто")
    if fmt == "авто":
        fmt = TEXT_EXTENSIONS.get(target.suffix.lower(), "txt")
    encoding = _encoding(params)
    append = bool(params.get("append"))

    if fmt == "json":
        if append:
            raise NodeError("дозапись JSON не поддерживается")
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        written = _write_bytes(target, text.encode(encoding, "replace"), False)
    elif fmt == "csv":
        delimiter = str(params.get("delimiter") or ";")
        if len(delimiter) != 1:
            raise NodeError("разделитель CSV должен быть одним символом")
        rows = _csv_rows(payload)
        fields = []
        for row in rows:
            for key in row:
                if key not in fields:
                    fields.append(key)
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fields, delimiter=delimiter,
                                extrasaction="ignore", lineterminator="\r\n")
        fresh = not append or not target.exists() or target.stat().st_size == 0
        if fresh:
            writer.writeheader()
        writer.writerows(rows)
        written = _write_bytes(target, buffer.getvalue().encode(encoding, "replace"), append)
    else:
        if isinstance(payload, str):
            text = payload
        else:
            text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        written = _write_bytes(target, text.encode(encoding, "replace"), append)

    return {"ok": {"path": str(target), "bytes": written,
                   "format": fmt, "append": append}}


# ------------------------------------------------------------------ HTTP
def _headers(raw):
    if raw in ("", None, {}):
        return {}
    try:
        data = json.loads(str(raw))
    except ValueError:
        raise NodeError("заголовки должны быть JSON-объектом")
    if not isinstance(data, dict) or len(data) > 50:
        raise NodeError("ожидался объект не длиннее 50 заголовков")
    prepared = {}
    for name, value in data.items():
        prepared[header_text(name, limit=64)] = header_text(value, limit=2048)
    return prepared


def _safe_http(url, *, method, timeout, headers=None, body=None, allow_private=False):
    try:
        return http_request(url, method=method, body=body, timeout=timeout,
                            headers=headers, allow_private=allow_private)
    except urllib.error.HTTPError as exc:
        raise NodeError(f"HTTP {exc.code} {exc.reason} от {url}")
    except urllib.error.URLError as exc:
        raise NodeError(f"сеть недоступна: {exc.reason}")
    except OSError as exc:
        raise NodeError(f"ошибка запроса: {exc}")


def _http_get(params):
    url = str(params.get("url") or "").strip()
    timeout = _int_param(params, "timeout", 10, 1, HTTP_TIMEOUT_LIMIT)
    headers = _headers(params.get("headers"))
    status, response_headers, text = _safe_http(
        url, method="GET", timeout=timeout, headers=headers,
        allow_private=_allow_private_urls())
    path = str(params.get("path") or "").strip()
    content_type = str(response_headers.get("Content-Type") or "").lower()
    value = text
    stripped = text.lstrip()
    looks_json = "json" in content_type or stripped[:1] in ("{", "[")
    if looks_json or path:
        try:
            parsed = json.loads(text)
        except ValueError:
            if path:
                raise NodeError("ответ не JSON, но задан путь к полю")
        else:
            check_json(parsed)
            value = parsed
    if path:
        value = resolve_path(value, path)
    if isinstance(value, str) and len(value) > MAX_HTTP_RESPONSE:
        value = value[:MAX_HTTP_RESPONSE]
    return {"out": value, "status": status}


# ------------------------------------------------------------------ обработка
def _foreach(params, payload, context):
    field = str(params.get("field") or "").strip()
    if field:
        data = _require_dict(payload, "Цикл")
        if field not in data:
            raise NodeError(f"поле «{field}» не найдено (есть: {_known_fields(data)})")
        source = data[field]
    else:
        source = payload
    if not isinstance(source, list):
        raise NodeError("ожидался массив для обхода")
    if len(source) > MAX_ARRAY_ITEMS:
        raise NodeError(f"массив длиннее {MAX_ARRAY_ITEMS} элементов")

    formula = str(params.get("formula") or "").strip()
    out_field = str(params.get("out_field") or "value").strip() or "value"
    result = []
    for index, item in enumerate(source):
        if context is not None:
            context.check()
        if not formula:
            result.append(item)
            continue
        scope = dict(item) if isinstance(item, dict) else {"item": item}
        scope.setdefault("item", item)
        scope["index"] = index
        value = evaluate(formula, scope)
        if isinstance(item, dict):
            copy = dict(item)
            copy[out_field] = value
            result.append(copy)
        else:
            result.append(value)
    return {"out": result}


def _aggregate(params, payload):
    field = str(params.get("field") or "").strip()
    if field:
        data = _require_dict(payload, "Агрегация")
        if field not in data:
            raise NodeError(f"поле «{field}» не найдено (есть: {_known_fields(data)})")
        source = data[field]
    else:
        source = payload
    if not isinstance(source, list):
        raise NodeError("ожидался массив для агрегации")
    if len(source) > MAX_ARRAY_ITEMS:
        raise NodeError(f"массив длиннее {MAX_ARRAY_ITEMS} элементов")

    op = str(params.get("op") or "сумма")
    if op == "количество":
        return {"out": len(source)}
    if op == "уникальные":
        unique, seen = [], set()
        for item in source:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return {"out": unique}
    if op == "склеить":
        separator = str(params.get("sep") or ", ")
        return {"out": separator.join(str(item) for item in source)[:MAX_FILE_BYTES]}

    numbers = []
    for item in source:
        number = _as_number(item)
        if number is None:
            raise NodeError(f"элемент {item!r} не является числом")
        numbers.append(number)
    if not numbers:
        return {"out": 0 if op == "сумма" else None}
    if op == "сумма":
        return {"out": _tidy(sum(numbers))}
    if op == "среднее":
        return {"out": _tidy(sum(numbers) / len(numbers))}
    if op == "минимум":
        return {"out": _tidy(min(numbers))}
    if op == "максимум":
        return {"out": _tidy(max(numbers))}
    raise NodeError(f"неизвестная операция агрегации: {op}")


def _regex(params, payload):
    data = _require_dict(payload, "Регулярное выражение")
    field = str(params.get("field") or "").strip()
    if field and field not in data:
        raise NodeError(f"поле «{field}» не найдено (есть: {_known_fields(data)})")
    subject = data[field] if field else data
    text = subject if isinstance(subject, str) else json.dumps(subject, ensure_ascii=False,
                                                            default=str)
    if len(text) > MAX_REGEX_SUBJECT:
        raise NodeError(f"значение длиннее {MAX_REGEX_SUBJECT} символов")

    flags = 0
    mode_flags = str(params.get("flags") or "нет")
    if mode_flags == "без регистра":
        flags |= re.IGNORECASE
    elif mode_flags == "многострочный":
        flags |= re.MULTILINE
    pattern = compile_regex(str(params.get("pattern") or ""))
    if flags:
        pattern = re.compile(pattern.pattern, pattern.flags | flags)

    mode = str(params.get("mode") or "извлечь")
    if mode == "извлечь":
        match = pattern.search(text)
        if match is None:
            value = None
        else:
            group = _int_param(params, "group", 0, 0, 9)
            try:
                value = match.group(group)
            except (IndexError, re.error):
                raise NodeError(f"в шаблоне нет группы {group}")
    elif mode == "заменить":
        replacement = str(params.get("replacement") or "")
        value = pattern.sub(replacement, text)
        if len(value) > MAX_REGEX_SUBJECT:
            value = value[:MAX_REGEX_SUBJECT]
    elif mode == "разделить":
        parts = pattern.split(text)
        if len(parts) > MAX_ARRAY_ITEMS:
            raise NodeError("слишком много частей после разделения")
        value = parts
    else:
        raise NodeError(f"неизвестное действие: {mode}")

    out_field = str(params.get("out_field") or "match").strip() or "match"
    result = dict(data)
    result[out_field] = value
    return {"out": result}


def _parse_datetime(value, fmt):
    if isinstance(value, bool):
        raise NodeError("дата не может быть логическим значением")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value or "").strip()
    if not text:
        raise NodeError("пустая дата")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    if fmt:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise NodeError(f"не удалось разобрать дату «{text[:40]}»")


def _datetime_node(params, payload):
    data = _require_dict(payload, "Дата и время")
    field = str(params.get("field") or "").strip()
    if field and field not in data:
        raise NodeError(f"поле «{field}» не найдено (есть: {_known_fields(data)})")
    value = data.get(field) if field else data
    fmt = str(params.get("format") or "")
    moment = _parse_datetime(value, fmt)
    action = str(params.get("action") or "разобрать")
    offset = _int_param(params, "tz_offset", 3, -12, 14)
    zone = timezone(timedelta(hours=offset))

    if action == "сдвинуть":
        unit = str(params.get("unit") or "дней")
        amount = _int_param(params, "amount", 0, -100_000, 100_000)
        steps = {"секунд": "seconds", "минут": "minutes",
                 "часов": "hours", "дней": "days"}
        if unit not in steps:
            raise NodeError(f"неизвестная единица сдвига: {unit}")
        moment = moment + timedelta(**{steps[unit]: amount})

    if action == "формат":
        if not fmt:
            raise NodeError("не задан формат вывода")
        output = moment.strftime(fmt)
    elif action in ("разобрать", "сдвинуть"):
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=zone)
        else:
            moment = moment.astimezone(zone)
        output = moment.isoformat(timespec="seconds")
    else:
        raise NodeError(f"неизвестное действие: {action}")

    out_field = str(params.get("out_field") or field or "datetime").strip() or "datetime"
    result = dict(data)
    result[out_field] = output
    return {"out": result}


# ------------------------------------------------------------------ SQLite
_SQL_FORBIDDEN = {"ATTACH", "DETACH", "PRAGMA", "VACUUM", "REINDEX"}
_SQL_READ_ONLY = {"SELECT", "WITH", "EXPLAIN"}


def _sqlite_bind(raw):
    try:
        data = json.loads(str(raw or "[]"))
    except ValueError:
        raise NodeError("параметры должны быть JSON-массивом или объектом")
    if isinstance(data, dict):
        prepared = {}
        for key, value in data.items():
            prepared[str(key)] = value
    elif isinstance(data, (list, tuple)):
        prepared = list(data)
    else:
        raise NodeError("параметры должны быть JSON-массивом или объектом")
    if len(prepared) > 100:
        raise NodeError("слишком много параметров SQL")
    for value in (prepared.values() if isinstance(prepared, dict) else prepared):
        if value is None or isinstance(value, (int, float, bool)):
            continue
        if isinstance(value, str) and len(value) <= MAX_CELL_CHARS:
            continue
        raise NodeError("параметры SQL должны быть простыми значениями")
    return prepared


def _sqlite(params):
    allow_write = bool(params.get("allow_write"))
    target = safe_path(_workspace(), params.get("db"), for_write=allow_write,
                       allowed_exts={".db", ".sqlite", ".sqlite3"})
    sql = str(params.get("sql") or "").strip()
    if not sql:
        raise NodeError("пустой SQL-запрос")
    if len(sql) > MAX_SQL_CHARS:
        raise NodeError("SQL-запрос слишком длинный")
    body = sql.rstrip(";")
    if ";" in body:
        raise NodeError("разрешён только один запрос за раз")
    match = re.match(r"\s*([A-Za-z]+)", body)
    keyword = match.group(1).upper() if match else ""
    if keyword in _SQL_FORBIDDEN:
        raise NodeError(f"{keyword} запрещён")
    if not allow_write and keyword not in _SQL_READ_ONLY:
        raise NodeError("запись в базу запрещена: включите «Разрешить запись»")

    bind = _sqlite_bind(params.get("params"))
    try:
        connection = sqlite3.connect(str(target), timeout=5)
    except sqlite3.Error as exc:
        raise NodeError(f"не удалось открыть базу: {exc}")
    try:
        connection.row_factory = sqlite3.Row
        try:
            connection.enable_load_extension(False)
        except AttributeError:
            pass
        if not allow_write:
            connection.execute("PRAGMA query_only = ON")
        deadline = time.monotonic() + 10
        connection.set_progress_handler(
            lambda: 1 if time.monotonic() > deadline else 0, 5000)
        try:
            cursor = connection.execute(body, bind)
            if cursor.description:
                rows = cursor.fetchmany(MAX_SQL_ROWS + 1)
                if len(rows) > MAX_SQL_ROWS:
                    raise NodeError(f"результат больше {MAX_SQL_ROWS} строк")
                value = [dict(row) for row in rows]
            else:
                connection.commit()
                value = {"changes": cursor.rowcount, "lastrowid": cursor.lastrowid}
        except sqlite3.OperationalError as exc:
            if "interrupt" in str(exc).lower():
                raise NodeError("запрос выполнялся дольше 10 секунд")
            raise NodeError(f"SQL: {exc}")
        except sqlite3.Error as exc:
            raise NodeError(f"SQL: {exc}")
    finally:
        connection.close()
    return {"out": value}


# ------------------------------------------------------------------ почта
def _send_mail(params, payload):
    cfg = _settings().get("mail", {})
    host = str(params.get("smtp_host") or os.environ.get("FLOWZA_SMTP_HOST")
               or cfg.get("smtp_host") or "").strip()
    host = header_text(host, limit=255, name="SMTP-сервер")
    if not host:
        raise NodeError("не задан SMTP-сервер: заполните «SMTP-сервер», "
                        "настройки или переменную FLOWZA_SMTP_HOST")
    if not re.fullmatch(r"[A-Za-z0-9.\-]+", host):
        raise NodeError("некорректное имя SMTP-сервера")

    raw_port = (params.get("smtp_port") or os.environ.get("FLOWZA_SMTP_PORT")
                or cfg.get("smtp_port") or 587)
    port = _int_param({"smtp_port": raw_port}, "smtp_port", 587, 1, 65535)

    user = str(params.get("smtp_user") or os.environ.get("FLOWZA_SMTP_USER")
               or cfg.get("smtp_user") or "").strip()
    user = header_text(user, limit=320, name="SMTP-пользователь")
    password_env = str(params.get("password_env") or cfg.get("password_env")
                       or "FLOWZA_SMTP_PASSWORD").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,99}", password_env):
        raise NodeError("некорректное имя переменной с паролем")
    password = os.environ.get(password_env, "")

    recipients = parse_emails(params.get("to"))
    if not recipients:
        raise NodeError("не указан получатель письма")
    copies = parse_emails(params.get("cc"))

    sender = str(params.get("from_addr") or cfg.get("from_addr") or user
                 or "flowza@localhost").strip()
    sender = header_text(sender, limit=320, name="От кого")
    if not re.match(r"^[^@\s<>]+@[^@\s<>]+$", sender):
        raise NodeError("некорректный адрес отправителя")

    subject = header_text(_fill_template(params.get("subject", ""), payload),
                          limit=200, name="Тема")
    body = safe_text(_fill_template(params.get("body", ""), payload),
                     limit=200_000, name="Текст письма")

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

    use_tls = bool(params.get("tls", cfg.get("tls", True)))
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
    method = str(params.get("method") or "POST").upper()
    if method not in ("POST", "PUT", "GET"):
        raise NodeError(f"неподдерживаемый метод: {method}")
    timeout = _int_param(params, "timeout", 10, 1, HTTP_TIMEOUT_LIMIT)

    body = None
    if method != "GET":
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        if len(body) > MAX_HTTP_BODY:
            raise NodeError("данные слишком большие для отправки")

    status, _headers_out, text = _safe_http(
        url, method=method, timeout=timeout, body=body,
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": "Flowza/1.0"},
        allow_private=_allow_private_urls())
    return {"ok": {"status": status, "url": url, "response": text[:2000]}}


# ------------------------------------------------------------------ выполнение
def run_node(node, inputs, context=None):
    """Выполнить одну ноду. Возвращает {имя_порта: значение}. Ошибки — NodeError."""
    try:
        return _run_node(node, inputs, context)
    except SecurityError as exc:
        raise NodeError(str(exc))


def _run_node(node, inputs, context):
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
        raw = params.get("json") or "{}"
        if len(str(raw)) > MAX_FILE_BYTES:
            raise NodeError("слишком большой JSON")
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise NodeError(f"невалидный JSON: {exc}")
        check_json(value)
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
            merged = left + right
            if len(merged) > MAX_ARRAY_ITEMS:
                raise NodeError(f"в списке больше {MAX_ARRAY_ITEMS} элементов")
            return {"out": merged}
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
        if context is not None:
            context.sleep(seconds)
        else:
            time.sleep(seconds)
        return {out["name"]: payload for out in outputs}

    if ty == "data.map":
        return {"out": evaluate(params.get("formula", ""), payload)}

    if ty == "data.rename":
        data = _require_dict(payload, "Переименование")
        old, new = params.get("from", ""), params.get("to", "")
        if not str(new).strip() or len(str(new)) > 200:
            raise NodeError("некорректное новое имя поля")
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
        separator = str(params.get("sep", " "))
        if len(separator) > 10:
            raise NodeError("разделитель слишком длинный")
        return {"out": separator.join(parts)[:MAX_FILE_BYTES]}

    if ty == "data.calculate":
        data = _require_dict(payload, "Вычисление поля")
        field = str(params.get("out_field") or "total").strip() or "total"
        if len(field) > 200:
            raise NodeError("слишком длинное имя поля")
        result = dict(data)
        result[field] = evaluate(params.get("expr", ""), data)
        return {"out": result}

    if ty == "data.foreach":
        return _foreach(params, payload, context)

    if ty == "data.aggregate":
        return _aggregate(params, payload)

    if ty == "data.regex":
        return _regex(params, payload)

    if ty == "data.datetime":
        return _datetime_node(params, payload)

    if ty == "io.file.read":
        return _file_read(params)

    if ty == "io.file.write":
        return _file_write(params, payload)

    if ty == "sqlite.query":
        return _sqlite(params)

    if ty == "http.get":
        return _http_get(params)

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
        label = safe_log_text(params.get("label", "событие"), limit=200)
        try:
            text = json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(payload)
        log.info("%s: %s", label, safe_log_text(text))
        return {"ok": {"logged": True, "label": label, "payload": payload}}

    return {out["name"]: payload for out in outputs}


# ------------------------------------------------------------------ доступ
def node_spec(node_type):
    return NODE_SPECS.get(node_type)


def all_node_types():
    return NODE_SPECS
