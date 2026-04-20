"""Tests for session_guard.py."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from broker.session_guard import is_session_conflict_error, connect_with_session_guard, SessionGuardResult
from tests.conftest import make_settings


class TestIsSessionConflictError:
    def test_detects_conflict_message(self):
        msg = "Error code=162 different ip address"
        assert is_session_conflict_error(msg) is True

    def test_case_insensitive(self):
        assert is_session_conflict_error("CODE=162 Different IP Address") is True

    def test_missing_code_162(self):
        assert is_session_conflict_error("different ip address only") is False

    def test_missing_ip_part(self):
        assert is_session_conflict_error("code=162 socket error") is False

    def test_empty_string(self):
        assert is_session_conflict_error("") is False

    def test_unrelated_error(self):
        assert is_session_conflict_error("Connection refused port 7497") is False

    def test_both_parts_required(self):
        assert is_session_conflict_error("code=162") is False
        assert is_session_conflict_error("different ip address") is False


class TestConnectWithSessionGuard:
    @pytest.fixture
    def settings(self):
        return make_settings(
            symbols=["AAPL"],
            session_retry_attempts=2,
            session_retry_delay_seconds=0,
            session_probe_duration="1 D",
            session_probe_bar_size="1 hour",
        )

    def test_successful_connection(self, settings):
        mock_client = MagicMock()
        mock_client.connect_and_start_any.return_value = 7497
        mock_client.get_historical_bars.return_value = [MagicMock()]

        with patch("broker.session_guard.IBClient", return_value=mock_client):
            result = connect_with_session_guard(settings, client_id=1001)

        assert isinstance(result, SessionGuardResult)
        assert result.client is mock_client
        assert result.port == 7497
        assert result.error is None
        assert len(result.attempts) == 1
        assert result.attempts[0].status == "ok"

    def test_connection_failure_returns_error(self, settings):
        mock_client = MagicMock()
        mock_client.connect_and_start_any.side_effect = ConnectionError("refused")

        with patch("broker.session_guard.IBClient", return_value=mock_client):
            result = connect_with_session_guard(settings, client_id=1001)

        assert result.client is None
        assert result.error is not None
        assert len(result.attempts) == 1
        assert result.attempts[0].status == "error"

    def test_session_conflict_retries(self, settings):
        call_count = 0
        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise Exception("code=162 different ip address")

        mock_client = MagicMock()
        mock_client.connect_and_start_any.side_effect = side_effect

        with patch("broker.session_guard.IBClient", return_value=mock_client), \
             patch("broker.session_guard.time.sleep"):
            result = connect_with_session_guard(settings, client_id=1001)

        # Should retry session_retry_attempts=2 times
        assert call_count == 2
        assert result.client is None
        assert any(a.status == "session_conflict" for a in result.attempts)

    def test_non_conflict_error_does_not_retry(self, settings):
        call_count = 0
        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise ConnectionError("generic error")

        mock_client = MagicMock()
        mock_client.connect_and_start_any.side_effect = side_effect

        with patch("broker.session_guard.IBClient", return_value=mock_client):
            result = connect_with_session_guard(settings, client_id=1001)

        # Should NOT retry — only 1 attempt
        assert call_count == 1
        assert result.attempts[0].status == "error"

    def test_probe_failure_triggers_disconnect(self, settings):
        mock_client = MagicMock()
        mock_client.connect_and_start_any.return_value = 7497
        mock_client.get_historical_bars.side_effect = TimeoutError("timed out")

        with patch("broker.session_guard.IBClient", return_value=mock_client):
            result = connect_with_session_guard(settings, client_id=1001)

        mock_client.disconnect_and_stop.assert_called()
        assert result.client is None
