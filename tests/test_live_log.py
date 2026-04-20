"""Tests for live_log.py (in-memory log handler)."""
from __future__ import annotations

import logging

import pytest

from broker.live_log import (
    MemoryLogHandler,
    clear_logs,
    get_recent_logs,
    install_memory_log_handler,
    _LOGS,
)


@pytest.fixture(autouse=True)
def reset_logs():
    clear_logs()
    yield
    clear_logs()


class TestGetRecentLogs:
    def test_empty_initially(self):
        assert get_recent_logs() == []

    def test_returns_zero_for_limit_zero(self):
        assert get_recent_logs(0) == []

    def test_returns_negative_limit_empty(self):
        assert get_recent_logs(-1) == []


class TestMemoryLogHandler:
    @pytest.fixture
    def handler(self):
        h = MemoryLogHandler()
        h.setFormatter(logging.Formatter("%(asctime)s"))
        return h

    def test_emit_appends_to_logs(self, handler):
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello world", args=(), exc_info=None
        )
        handler.emit(record)
        logs = get_recent_logs()
        assert len(logs) == 1
        assert logs[0]["message"] == "hello world"
        assert logs[0]["level"] == "INFO"
        assert logs[0]["logger"] == "test"

    def test_multiple_records_stored_in_order(self, handler):
        for i in range(5):
            record = logging.LogRecord(
                name="t", level=logging.WARNING, pathname="", lineno=0,
                msg=f"msg {i}", args=(), exc_info=None
            )
            handler.emit(record)
        logs = get_recent_logs()
        messages = [l["message"] for l in logs]
        assert messages == [f"msg {i}" for i in range(5)]

    def test_max_500_entries(self, handler):
        for i in range(510):
            record = logging.LogRecord(
                name="t", level=logging.DEBUG, pathname="", lineno=0,
                msg=f"msg {i}", args=(), exc_info=None
            )
            handler.emit(record)
        logs = get_recent_logs(limit=600)
        assert len(logs) <= 500

    def test_limit_respected(self, handler):
        for i in range(20):
            record = logging.LogRecord(
                name="t", level=logging.INFO, pathname="", lineno=0,
                msg=f"msg {i}", args=(), exc_info=None
            )
            handler.emit(record)
        logs = get_recent_logs(limit=5)
        assert len(logs) == 5
        # Should return the most recent 5
        assert logs[-1]["message"] == "msg 19"

    def test_clear_empties_logs(self, handler):
        record = logging.LogRecord(
            name="t", level=logging.INFO, pathname="", lineno=0,
            msg="test", args=(), exc_info=None
        )
        handler.emit(record)
        assert len(get_recent_logs()) == 1
        clear_logs()
        assert get_recent_logs() == []


class TestInstallMemoryLogHandler:
    def test_installs_handler_on_root_logger(self):
        root = logging.getLogger()
        original_count = len(root.handlers)
        install_memory_log_handler("INFO")
        assert len(root.handlers) >= original_count

    def test_idempotent_multiple_calls(self):
        root = logging.getLogger()
        install_memory_log_handler("INFO")
        count_after_first = len(root.handlers)
        install_memory_log_handler("INFO")
        install_memory_log_handler("DEBUG")
        assert len(root.handlers) == count_after_first
