from __future__ import annotations

import hashlib
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from .errors import ExchangeValidationError
from .exchange import BaseExchangeAdapter, LiveExchangeAdapter, OrderRequest, OrderStatus, PaperExchangeAdapter


@dataclass
class ManagedOrder:
    intent_key: str
    order: OrderRequest
    status: OrderStatus


class OrderManager:
    def __init__(self, adapter: BaseExchangeAdapter, max_open_orders: int, max_cancels_per_sec: int) -> None:
        self.adapter = adapter
        self.max_open_orders = max_open_orders
        self.max_cancels_per_sec = max_cancels_per_sec
        self.managed: dict[str, ManagedOrder] = {}
        self.by_client_id: dict[str, ManagedOrder] = {}
        self.cancel_timestamps: deque[float] = deque()

    def _client_id_for_intent(self, intent_key: str) -> str:
        h = hashlib.sha256(intent_key.encode("utf-8")).hexdigest()[:20]
        return f"cx-{h}"

    def submit_limit(self, intent_key: str, symbol: str, side: str, price: Decimal, qty: Decimal,
                     tif: str, post_only: bool, reduce_only: bool = False, market_mid: Decimal | None = None) -> ManagedOrder:
        if intent_key in self.managed and self.managed[intent_key].status.status in {"OPEN", "PARTIALLY_FILLED"}:
            return self.managed[intent_key]

        if self.open_order_count() >= self.max_open_orders:
            raise ExchangeValidationError("max_open_orders reached")

        req = OrderRequest(
            client_order_id=self._client_id_for_intent(intent_key),
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            price=price,
            qty=qty,
            order_type="limit",
            time_in_force=tif,
            post_only=post_only,
            reduce_only=reduce_only,
        )
        if isinstance(self.adapter, PaperExchangeAdapter):
            if market_mid is None:
                raise ExchangeValidationError("market_mid is required in paper mode")
            status = self.adapter.place_order(req, market_mid=market_mid)
        elif isinstance(self.adapter, LiveExchangeAdapter):
            status = self.adapter.place_order(req)
        else:
            raise ExchangeValidationError("unsupported adapter")

        managed = ManagedOrder(intent_key=intent_key, order=req, status=status)
        self.managed[intent_key] = managed
        self.by_client_id[req.client_order_id] = managed
        return managed

    def cancel(self, intent_key: str) -> None:
        if intent_key not in self.managed:
            return
        now = time.time()
        while self.cancel_timestamps and now - self.cancel_timestamps[0] > 1.0:
            self.cancel_timestamps.popleft()
        if len(self.cancel_timestamps) >= self.max_cancels_per_sec:
            return

        managed = self.managed[intent_key]
        self.cancel_timestamps.append(now)
        managed.status = self.adapter.cancel_order(managed.status.exchange_order_id)

    def apply_order_update(self, update: OrderStatus) -> None:
        managed = self.by_client_id.get(update.client_order_id)
        if not managed:
            return
        if managed.status.status == "FILLED" and update.status == "CANCELED":
            return
        managed.status = update

    def reconcile_open_orders(self) -> tuple[list[str], list[str]]:
        exchange_open_ids = {o.client_order_id for o in self.adapter.fetch_open_orders()}
        local_open = {
            m.order.client_order_id
            for m in self.managed.values()
            if m.status.status in {"OPEN", "PARTIALLY_FILLED"}
        }
        unknown_exchange = sorted(exchange_open_ids - local_open)
        missing_local = sorted(local_open - exchange_open_ids)
        return unknown_exchange, missing_local

    def open_order_count(self) -> int:
        return sum(1 for m in self.managed.values() if m.status.status in {"OPEN", "PARTIALLY_FILLED"})
