from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass
from queue import Empty
from queue import Queue
from typing import Any

from ibapi.client import EClient
from ibapi.common import BarData
from ibapi.contract import Contract
from ibapi.contract import ContractDetails
from ibapi.order import Order
from ibapi.wrapper import EWrapper


@dataclass(frozen=True)
class HistoricalBar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class NewsHeadline:
    provider_code: str
    article_id: str
    headline: str
    timestamp: str


class IBClient(EWrapper, EClient):
    ROUTINE_INFO_CODES = {2104, 2106, 2107, 2108, 2158}
    QUIET_WARNING_CODES = {2176}
    NON_FATAL_WARNING_CODES = ROUTINE_INFO_CODES | QUIET_WARNING_CODES

    def __init__(self) -> None:
        EWrapper.__init__(self)
        EClient.__init__(self, self)
        self.logger = logging.getLogger(self.__class__.__name__)
        self._thread: threading.Thread | None = None
        self._ready_event = threading.Event()
        self._req_id = 1
        self._req_id_lock = threading.Lock()
        self._order_id: int | None = None
        self._request_errors: dict[int, Queue[str]] = defaultdict(Queue)
        self._historical_data: dict[int, list[HistoricalBar]] = defaultdict(list)
        self._historical_end: dict[int, threading.Event] = defaultdict(threading.Event)
        self._contract_details: dict[int, list[ContractDetails]] = defaultdict(list)
        self._contract_details_end: dict[int, threading.Event] = defaultdict(threading.Event)
        self._historical_news: dict[int, list[NewsHeadline]] = defaultdict(list)
        self._historical_news_end: dict[int, threading.Event] = defaultdict(threading.Event)
        self._news_providers: list[Any] = []
        self._news_providers_event = threading.Event()
        self._positions: dict[str, float] = {}
        self._avg_costs: dict[str, float] = {}
        self._positions_end = threading.Event()
        self.connected_port: int | None = None

    def connect_and_start(self, host: str, port: int, client_id: int, timeout: float = 10.0) -> None:
        self._ready_event.clear()
        self._clear_request_error(-1)
        self.connected_port = None
        self.connect(host, port, client_id)
        self._thread = threading.Thread(target=self.run, name="ibapi-network", daemon=True)
        self._thread.start()

        if not self._ready_event.wait(timeout=timeout):
            connection_error = self._pop_connection_error()
            if connection_error:
                raise ConnectionError(
                    f"Unable to connect to IBKR on port {port}: {connection_error}. "
                    "Confirm TWS or IB Gateway is running, API is enabled, and the socket port matches."
                )
            raise TimeoutError(f"Timed out connecting to IBKR on port {port}; nextValidId was not received")

        self.connected_port = port
        self.logger.info("Connected to IBKR on port=%s", port)

    def connect_and_start_any(self, host: str, ports: list[int], client_id: int, timeout: float = 10.0) -> int:
        errors: list[str] = []
        seen: set[int] = set()
        ordered_ports = [port for port in ports if not (port in seen or seen.add(port))]

        for port in ordered_ports:
            try:
                self.logger.info("Trying IBKR host=%s port=%s clientId=%s", host, port, client_id)
                self.connect_and_start(host, port, client_id, timeout=timeout)
                return port
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{port}: {exc}")
                self.disconnect_and_stop()

        raise ConnectionError("All IBKR connection attempts failed: " + " | ".join(errors))

    def disconnect_and_stop(self) -> None:
        if self.isConnected():
            self.disconnect()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self.connected_port = None
        self.logger.info("IBKR connection closed")

    def nextValidId(self, orderId: int) -> None:  # noqa: N802
        self._order_id = orderId
        self._ready_event.set()
        self.logger.info("Received nextValidId=%s", orderId)

    def error(self, reqId: int, errorCode: int, errorString: str, advancedOrderRejectJson: str = "") -> None:  # noqa: N802
        message = f"reqId={reqId}, code={errorCode}, msg={errorString}"
        if advancedOrderRejectJson:
            message = f"{message}, details={advancedOrderRejectJson}"

        if errorCode in self.ROUTINE_INFO_CODES:
            self.logger.debug(message)
            return

        if errorCode in self.QUIET_WARNING_CODES:
            self.logger.debug(message)
            return

        if errorCode in self.NON_FATAL_WARNING_CODES:
            self.logger.info(message)
            return

        self.logger.warning(message)
        self._request_errors[reqId].put(message)

    def historicalData(self, reqId: int, bar: BarData) -> None:  # noqa: N802
        self._historical_data[reqId].append(
            HistoricalBar(
                date=bar.date,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
        )

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:  # noqa: N802
        self._historical_end[reqId].set()

    def contractDetails(self, reqId: int, contractDetails: ContractDetails) -> None:  # noqa: N802
        self._contract_details[reqId].append(contractDetails)

    def contractDetailsEnd(self, reqId: int) -> None:  # noqa: N802
        self._contract_details_end[reqId].set()

    def newsProviders(self, newsProviders: list[Any]) -> None:  # noqa: N802
        self._news_providers = list(newsProviders)
        self._news_providers_event.set()

    def historicalNews(self, reqId: int, time: str, providerCode: str, articleId: str, headline: str) -> None:  # noqa: N802
        self._historical_news[reqId].append(
            NewsHeadline(
                provider_code=providerCode,
                article_id=articleId,
                headline=headline,
                timestamp=time,
            )
        )

    def historicalNewsEnd(self, reqId: int, hasMore: bool) -> None:  # noqa: N802
        self._historical_news_end[reqId].set()

    def position(self, account: str, contract: Contract, position: float, avgCost: float) -> None:  # noqa: N802
        self._positions[contract.symbol] = position
        if avgCost != 0:
            self._avg_costs[contract.symbol] = avgCost

    def positionEnd(self) -> None:  # noqa: N802
        self._positions_end.set()

    def orderStatus(  # noqa: N802
        self,
        orderId: int,
        status: str,
        filled: float,
        remaining: float,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float,
    ) -> None:
        self.logger.info(
            "Order status orderId=%s status=%s filled=%s remaining=%s avgFillPrice=%s",
            orderId,
            status,
            filled,
            remaining,
            avgFillPrice,
        )

    def next_request_id(self) -> int:
        with self._req_id_lock:
            req_id = self._req_id
            self._req_id += 1
        return req_id

    def next_order_id(self) -> int:
        if self._order_id is None:
            raise RuntimeError("nextValidId has not been received yet; cannot place orders")
        order_id = self._order_id
        self._order_id += 1
        return order_id

    def create_stock_contract(self, symbol: str) -> Contract:
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        return contract

    def get_historical_bars(
        self,
        symbol: str,
        duration: str,
        bar_size: str,
        use_rth: bool,
        timeout: float = 15.0,
    ) -> list[HistoricalBar]:
        req_id = self.next_request_id()
        self._historical_data[req_id].clear()
        self._historical_end[req_id].clear()
        self._clear_request_error(req_id)

        self.reqHistoricalData(
            req_id,
            self.create_stock_contract(symbol),
            "",
            duration,
            bar_size,
            "TRADES",
            1 if use_rth else 0,
            1,
            False,
            [],
        )

        self._wait_for_request_completion(
            req_id=req_id,
            event=self._historical_end[req_id],
            timeout=timeout,
            timeout_message=f"Historical data request timed out for {symbol}",
        )
        bars = list(self._historical_data[req_id])
        if not bars:
            raise RuntimeError(f"No historical data returned for {symbol}")
        return bars

    def get_contract_con_id(self, symbol: str, timeout: float = 10.0) -> int:
        req_id = self.next_request_id()
        self._contract_details[req_id].clear()
        self._contract_details_end[req_id].clear()
        self._clear_request_error(req_id)
        self.reqContractDetails(req_id, self.create_stock_contract(symbol))

        self._wait_for_request_completion(
            req_id=req_id,
            event=self._contract_details_end[req_id],
            timeout=timeout,
            timeout_message=f"Contract details request timed out for {symbol}",
        )
        details = self._contract_details[req_id]
        if not details:
            raise RuntimeError(f"No contract details returned for {symbol}")
        return details[0].contract.conId

    def get_news_providers(self, timeout: float = 5.0) -> list[Any]:
        self._news_providers = []
        self._news_providers_event.clear()
        self.reqNewsProviders()

        if not self._news_providers_event.wait(timeout=timeout):
            raise TimeoutError("News provider request timed out")

        return list(self._news_providers)

    def get_recent_news(
        self,
        symbol: str,
        provider_codes: list[str],
        max_items: int,
        timeout: float = 10.0,
    ) -> list[NewsHeadline]:
        con_id = self.get_contract_con_id(symbol)
        req_id = self.next_request_id()
        self._historical_news[req_id].clear()
        self._historical_news_end[req_id].clear()
        self._clear_request_error(req_id)

        self.reqHistoricalNews(
            req_id,
            con_id,
            "+".join(provider_codes),
            "",
            "",
            max_items,
            [],
        )

        self._wait_for_request_completion(
            req_id=req_id,
            event=self._historical_news_end[req_id],
            timeout=timeout,
            timeout_message=f"Historical news request timed out for {symbol}",
        )
        return list(self._historical_news[req_id])

    def get_positions(self, timeout: float = 10.0) -> dict[str, float]:
        self._positions = {}
        self._positions_end.clear()
        self.reqPositions()

        if not self._positions_end.wait(timeout=timeout):
            raise TimeoutError("Position request timed out")

        return dict(self._positions)

    def get_avg_costs(self) -> dict[str, float]:
        """Return average cost basis for each symbol with a position."""
        return dict(self._avg_costs)

    def place_market_order(self, symbol: str, action: str, quantity: int) -> int:
        order = Order()
        order.action = action
        order.orderType = "MKT"
        order.totalQuantity = quantity
        order.transmit = True

        order_id = self.next_order_id()
        self.placeOrder(order_id, self.create_stock_contract(symbol), order)
        self.logger.info("Submitted order orderId=%s symbol=%s action=%s qty=%s", order_id, symbol, action, quantity)
        return order_id

    def _raise_request_error_if_any(self, req_id: int) -> None:
        queue = self._request_errors.get(req_id)
        if queue and not queue.empty():
            raise RuntimeError(queue.get())

    def _wait_for_request_completion(
        self,
        req_id: int,
        event: threading.Event,
        timeout: float,
        timeout_message: str,
    ) -> None:
        remaining = timeout
        interval = 0.2

        while remaining > 0:
            if event.wait(timeout=min(interval, remaining)):
                self._raise_request_error_if_any(req_id)
                return
            self._raise_request_error_if_any(req_id)
            remaining -= interval

        self._raise_request_error_if_any(req_id)
        raise TimeoutError(timeout_message)

    def _pop_connection_error(self) -> str | None:
        queue = self._request_errors.get(-1)
        if queue and not queue.empty():
            return queue.get()
        return None

    def _clear_request_error(self, req_id: int) -> None:
        queue = self._request_errors.get(req_id)
        if not queue:
            return
        while True:
            try:
                queue.get_nowait()
            except Empty:
                break
