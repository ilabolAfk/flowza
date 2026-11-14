"""Настройки Flowza: значения по умолчанию, проверка и атомарное сохранение.

Файл настроек лежит рядом с графом: в портативной сборке — в «Документы/Flowza»,
в репозитории — в корне проекта. Секреты в файл не пишутся: пароль SMTP и токен
вебхука берутся из переменных окружения.
"""

import json
import os
import sys
import threading
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PKG_DIR.parent


def data_dir():
    override = os.environ.get("FLOWZA_GRAPH")
    if override:
        return Path(override).expanduser().resolve().parent
    if getattr(sys, "frozen", False):
        return Path.home() / "Documents" / "Flowza"
    return PROJECT_DIR


def graph_path():
    override = os.environ.get("FLOWZA_GRAPH")
    if override:
        return Path(override).expanduser().resolve()
    return data_dir() / "flowza_graph.json"


def settings_path():
    return data_dir() / "flowza_settings.json"


def runs_log_path():
    return data_dir() / "flowza_runs.log"


def default_workspace():
    return data_dir() / "workspace"


FIELDS = {
    ("run", "pause_ms"): ("int", 0, 0, 5000),
    ("run", "timeout_s"): ("int", 120, 5, 3600),
    ("run", "on_error"): ("choice", "continue", ("continue", "stop"), None),
    ("run", "retries"): ("int", 0, 0, 3),
    ("run", "autosave_before_run"): ("bool", True, None, None),
    ("files", "autosave_sec"): ("int", 0, 0, 600),
    ("files", "backups"): ("int", 5, 0, 50),
    ("files", "workspace"): ("text", "", 0, 500),
    ("mail", "smtp_host"): ("text", "", 0, 255),
    ("mail", "smtp_port"): ("int", 587, 1, 65535),
    ("mail", "smtp_user"): ("text", "", 0, 255),
    ("mail", "from_addr"): ("text", "", 0, 255),
    ("mail", "tls"): ("bool", True, None, None),
    ("mail", "password_env"): ("text", "FLOWZA_SMTP_PASSWORD", 1, 100),
    ("ui", "theme"): ("choice", "dark", ("dark", "light"), None),
    ("ui", "grid"): ("int", 24, 8, 64),
    ("ui", "snap"): ("bool", False, None, None),
    ("ui", "minimap"): ("bool", True, None, None),
    ("webhook", "enabled"): ("bool", True, None, None),
    ("webhook", "token_env"): ("text", "FLOWZA_WEBHOOK_TOKEN", 1, 100),
    ("security", "allow_private_urls"): ("bool", False, None, None),
}

SECTION_TITLES = {
    "run": "Прогон",
    "files": "Файлы",
    "mail": "Почта",
    "ui": "Интерфейс",
    "webhook": "Вебхук",
    "security": "Безопасность",
}

_current = None
_current_lock = threading.Lock()


def defaults():
    result = {}
    for (section, key), spec in FIELDS.items():
        result.setdefault(section, {})[key] = spec[1]
    return result


def _coerce(kind, value, default, low, high, where):
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes", "on"):
            return True
        if isinstance(value, str) and value.strip().lower() in ("false", "0", "no", "off"):
            return False
        return default
    if kind == "int":
        if isinstance(value, bool):
            return default
        try:
            number = int(float(str(value).strip().replace(" ", "")))
        except (TypeError, ValueError):
            return default
        return max(low, min(high, number))
    if kind == "choice":
        text = str(value).strip().lower() if value is not None else ""
        return text if text in low else default
    text = "" if value is None else str(value).strip()
    text = text.replace("\r", " ").replace("\n", " ")
    if len(text) > high:
        text = text[:high]
    if low and len(text) < low:
        return default
    return text


def normalize(raw):
    """Привести произвольный словарь к схеме. Возвращает (настройки, замечания)."""
    settings = defaults()
    warnings = []
    if not isinstance(raw, dict):
        return settings, ["настройки должны быть объектом: взяты значения по умолчанию"]
    known = {section: set() for section, _ in FIELDS}
    for (section, key), spec in FIELDS.items():
        if section not in raw or not isinstance(raw.get(section), dict):
            continue
        if key not in raw[section]:
            continue
        kind, default, arg1, arg2 = spec
        value = raw[section][key]
        if kind == "choice":
            coerced = _coerce("choice", value, default, arg1, None, f"{section}.{key}")
        elif kind == "int":
            coerced = _coerce("int", value, default, arg1, arg2, f"{section}.{key}")
        elif kind == "bool":
            coerced = _coerce("bool", value, default, None, None, f"{section}.{key}")
        else:
            coerced = _coerce("text", value, default, arg1, arg2, f"{section}.{key}")
        if kind in ("int", "bool", "choice") and coerced == default and value not in (default, None):
            text = str(value).strip().lower()
            if kind != "int" or not text.lstrip("-").isdigit():
                warnings.append(f"{section}.{key}: значение {value!r} заменено на {default!r}")
        settings[section][key] = coerced
        known[section].add(key)
    for section, value in raw.items():
        if section not in SECTION_TITLES or not isinstance(value, dict):
            continue
        for key in value:
            if key not in known[section]:
                warnings.append(f"неизвестная настройка {section}.{key} проигнорирована")
    return settings, warnings


def load(path=None):
    target = Path(path) if path else settings_path()
    if not target.exists():
        return defaults(), []
    try:
        with open(target, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        return defaults(), [f"не удалось прочитать настройки: {exc}"]
    return normalize(raw)


def save(settings, path=None):
    target = Path(path) if path else settings_path()
    clean, _ = normalize(settings)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(clean, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    return clean


def workspace(settings=None):
    settings = settings or current()
    raw = str(settings.get("files", {}).get("workspace") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return default_workspace()


def describe_env(settings):
    """Какие секреты заданы в окружении — без самих значений."""
    password_env = settings.get("mail", {}).get("password_env") or "FLOWZA_SMTP_PASSWORD"
    token_env = settings.get("webhook", {}).get("token_env") or "FLOWZA_WEBHOOK_TOKEN"
    return {
        "smtp_password_env": password_env,
        "smtp_password_set": bool(os.environ.get(password_env)),
        "webhook_token_env": token_env,
        "webhook_token_set": bool(os.environ.get(token_env)),
        "smtp_host_env": bool(os.environ.get("FLOWZA_SMTP_HOST")),
    }


def public(settings):
    payload = normalize(settings)[0]
    payload["env"] = describe_env(payload)
    payload["files"]["workspace_resolved"] = str(workspace(payload))
    payload["graph_file"] = str(graph_path())
    payload["settings_file"] = str(settings_path())
    payload["sections"] = SECTION_TITLES
    return payload


def current():
    with _current_lock:
        settings = _current
    if settings is None:
        return defaults()
    return settings


def set_current(settings):
    global _current
    clean, _ = normalize(settings)
    with _current_lock:
        _current = clean
    return clean


class SettingsStore:
    """Потокобезопасное хранилище настроек с сохранением на диск."""

    def __init__(self, path=None):
        self.path = Path(path) if path else settings_path()
        self._lock = threading.RLock()
        self._settings, self.warnings = load(self.path)
        set_current(self._settings)

    def get(self):
        with self._lock:
            return json.loads(json.dumps(self._settings))

    def update(self, patch):
        with self._lock:
            merged = json.loads(json.dumps(self._settings))
            if not isinstance(patch, dict):
                raise ValueError("ожидался объект настроек")
            for section, values in patch.items():
                if section in ("env", "sections", "graph_file", "settings_file"):
                    continue
                if not isinstance(values, dict):
                    continue
                merged.setdefault(section, {}).update(values)
            clean, warnings = normalize(merged)
            saved = save(clean, self.path)
            self._settings = saved
            self.warnings = warnings
            set_current(saved)
            return self.get(), warnings

    def reload(self):
        with self._lock:
            self._settings, self.warnings = load(self.path)
            set_current(self._settings)
            return self.get()
