from __future__ import annotations

import time
from dataclasses import dataclass

from broker.config import Settings
from broker.ib_client import IBClient
from broker.models import SessionAttempt


@dataclass
class SessionGuardResult:
    client: IBClient | None
    port: int | None
    attempts: list[SessionAttempt]
    error: str | None = None


def is_session_conflict_error(message: str) -> bool:
    lowered = message.lower()
    return "code=162" in lowered and "different ip address" in lowered


def connect_with_session_guard(settings: Settings, client_id: int) -> SessionGuardResult:
    attempts: list[SessionAttempt] = []
    probe_symbol = settings.symbols[0]

    for attempt_no in range(1, settings.session_retry_attempts + 1):
        client = IBClient()
        try:
            port = client.connect_and_start_any(
                settings.ib_host,
                settings.ib_port_candidates,
                client_id,
            )
            client.get_historical_bars(
                symbol=probe_symbol,
                duration=settings.session_probe_duration,
                bar_size=settings.session_probe_bar_size,
                use_rth=settings.use_rth,
                timeout=15.0,
            )
            attempts.append(SessionAttempt(attempt=attempt_no, status="ok", message=f"Session probe succeeded on port {port}"))
            return SessionGuardResult(client=client, port=port, attempts=attempts)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            status = "session_conflict" if is_session_conflict_error(message) else "error"
            attempts.append(SessionAttempt(attempt=attempt_no, status=status, message=message))
            client.disconnect_and_stop()

            if status == "session_conflict" and attempt_no < settings.session_retry_attempts:
                time.sleep(settings.session_retry_delay_seconds)
                continue

            return SessionGuardResult(client=None, port=None, attempts=attempts, error=message)

    return SessionGuardResult(client=None, port=None, attempts=attempts, error="Session guard exhausted all retries")
