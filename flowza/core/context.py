"""Контекст прогона: отмена, дедлайн и ожидание с проверками."""

import time

CANCELLED = "cancelled"
TIMEOUT = "timeout"


class RunAborted(Exception):
    """Прогон прерван: пользователем или по таймауту."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


class RunContext:
    """Общее состояние прогона для нод: отмена, дедлайн, безопасный sleep."""

    def __init__(self, cancel=None, deadline=None):
        self.cancel = cancel
        self.deadline = deadline

    def cancelled(self):
        return bool(self.cancel is not None and self.cancel.is_set())

    def expired(self):
        return self.deadline is not None and time.monotonic() >= self.deadline

    def check(self):
        if self.cancelled():
            raise RunAborted(CANCELLED, "прогон отменён пользователем")
        if self.expired():
            raise RunAborted(TIMEOUT, "превышен таймаут прогона")

    def sleep(self, seconds):
        seconds = float(seconds or 0)
        if seconds <= 0:
            self.check()
            return
        end = time.monotonic() + seconds
        while True:
            self.check()
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))
