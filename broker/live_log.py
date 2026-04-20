from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Deque

_LOCK = threading.Lock()
_LOGS: Deque[dict] = deque(maxlen=500)
_HANDLER_NAME = "broker-memory-log-handler"


class MemoryLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        item = {
            "timestamp": self.formatter.formatTime(record, "%H:%M:%S") if self.formatter else "",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        with _LOCK:
            _LOGS.append(item)


def install_memory_log_handler(level: str) -> None:
    root = logging.getLogger()
    existing = next((handler for handler in root.handlers if getattr(handler, "name", "") == _HANDLER_NAME), None)
    if existing is not None:
        existing.setLevel(getattr(logging, level, logging.INFO))
        return

    handler = MemoryLogHandler(level=getattr(logging, level, logging.INFO))
    handler.name = _HANDLER_NAME
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
    root.addHandler(handler)


def get_recent_logs(limit: int = 200) -> list[dict]:
    with _LOCK:
        if limit <= 0:
            return []
        return list(_LOGS)[-limit:]


def clear_logs() -> None:
    with _LOCK:
        _LOGS.clear()
