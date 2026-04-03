from __future__ import annotations

import hashlib
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from .errors import ExchangeTransientError, ExchangeValidationError, RateLimitExceededError
from .exchange import BaseExchangeAdapter, LiveExchangeAdapter, OrderRequest, OrderStatus, PaperExchangeAdapter


@dataclass
class ManagedOrder:
    intent_key: str
    order: OrderRequest
    status: OrderStatus
    last_replace_ts: float = 0.0


class OrderManager:
    def __init__(
        self,
        adapter: BaseExchangeAdapter,
        max_open_orders: int,
        max_cancels_per_sec: int,
        max_new_orders_per_sec: int,
        max_retries: int,
        retry_backoff_ms: int,
        min_replace_interval_ms: int,
    ) -> None:
        self.adapter = adapter
        self.max_open_orders = max_open_orders
        self.max_cancels_per_sec = max_cancels_per_sec
        self.max_new_orders_per_sec = max_new_orders_per_sec
        self.max_retries = max_retries
        self.retry_backoff_ms = retry_backoff_ms
        self.min_replace_interval_ms = min_replace_interval_ms
        self.managed: dict[str, ManagedOrder] = {}
        self.by_client_id: dict[str, ManagedOrder] = {}
        self.cancel_timestamps: deque[float] = deque()
        self.new_order_timestamps: deque[float] = deque()

    def _client_id_for_intent(self, intent_key: str) -> str:
        h = hashlib.sha256(intent_key.encode("utf-8")).hexdigest()[:20]
        return f"cx-{h}"

    @staticmethod
    def _cleanup_window(buf: deque[float], now: float) -> None:
        while buf and now - buf[0] > 1.0:
            buf.popleft()

    def _enforce_new_order_rate(self) -> None:
        now = time.time()
        self._cleanup_window(self.new_order_timestamps, now)
        if len(self.new_order_timestamps) >= self.max_new_orders_per_sec:
            raise RateLimitExceededError("max new orders per second reached")
        self.new_order_timestamps.append(now)

    def submit_limit(
        self,
        intent_key: str,
        symbol: str,
        side: str,
        price: Decimal,
        qty: Decimal,
        tif: str,
        post_only: bool,
        reduce_only: bool = False,
        market_mid: Decimal | None = None,
    ) -> ManagedOrder:
        existing = self.managed.get(intent_key)
        if existing and existing.status.status in {"OPEN", "PARTIALLY_FILLED"}:
            return existing

        if self.open_order_count() >= self.max_open_orders:
            raise ExchangeValidationError("max_open_orders reached")
        self._enforce_new_order_rate()

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

        if post_only and market_mid is not None:
            if (side == "BUY" and req.price >= market_mid) or (side == "SELL" and req.price <= market_mid):
                raise ExchangeValidationError("post-only order would cross market mid")

        status = self._place_with_retry(req, market_mid)
        managed = ManagedOrder(intent_key=intent_key, order=req, status=status)
        self.managed[intent_key] = managed
        self.by_client_id[req.client_order_id] = managed
        return managed

    def _place_with_retry(self, req: OrderRequest, market_mid: Decimal | None) -> OrderStatus:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                if isinstance(self.adapter, PaperExchangeAdapter):
                    if market_mid is None:
                        raise ExchangeValidationError("market_mid is required in paper mode")
                    return self.adapter.place_order(req, market_mid=market_mid)
                if isinstance(self.adapter, LiveExchangeAdapter):
                    return self.adapter.place_order(req)
                raise ExchangeValidationError("unsupported adapter")
            except ExchangeTransientError as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    break
                time.sleep((self.retry_backoff_ms / 1000.0) * (2 ** attempt))
        if last_exc:
            raise last_exc
        raise ExchangeValidationError("unexpected order placement failure")

    def cancel(self, intent_key: str) -> None:
        if intent_key not in self.managed:
            return
        now = time.time()
        self._cleanup_window(self.cancel_timestamps, now)
        if len(self.cancel_timestamps) >= self.max_cancels_per_sec:
            raise RateLimitExceededError("max cancels per second reached")

        managed = self.managed[intent_key]
        self.cancel_timestamps.append(now)
        managed.status = self.adapter.cancel_order(managed.status.exchange_order_id)

    def cancel_replace(self, intent_key: str, new_price: Decimal, new_qty: Decimal, market_mid: Decimal | None = None) -> ManagedOrder:
        managed = self.managed.get(intent_key)
        if not managed:
            raise ExchangeValidationError("cannot replace unknown intent")
        if managed.status.status not in {"OPEN", "PARTIALLY_FILLED"}:
            return managed

        now = time.time()
        if (now - managed.last_replace_ts) * 1000 < self.min_replace_interval_ms:
            return managed

        if managed.status.status == "PARTIALLY_FILLED":
            remaining = managed.order.qty - managed.status.filled_qty
            if remaining <= Decimal("0"):
                return managed
            new_qty = min(new_qty, remaining)

        self.cancel(intent_key)
        managed.last_replace_ts = now
        return self.submit_limit(
            intent_key=intent_key,
            symbol=managed.order.symbol,
            side=managed.order.side,
            price=new_price,
            qty=new_qty,
            tif=managed.order.time_in_force,
            post_only=managed.order.post_only,
            reduce_only=managed.order.reduce_only,
            market_mid=market_mid,
        )

    def apply_order_update(self, update: OrderStatus) -> None:
        managed = self.by_client_id.get(update.client_order_id)
        if not managed:
            return
        if managed.status.status == "FILLED" and update.status == "CANCELED":
            return
        managed.status = update

    def reconcile_open_orders(self, cancel_unknown: bool = True) -> tuple[list[str], list[str]]:
        exchange_open = self.adapter.fetch_open_orders()
        exchange_open_ids = {o.client_order_id for o in exchange_open}
        local_open = {
            m.order.client_order_id
            for m in self.managed.values()
            if m.status.status in {"OPEN", "PARTIALLY_FILLED"}
        }
        unknown_exchange = sorted(exchange_open_ids - local_open)
        missing_local = sorted(local_open - exchange_open_ids)

        if cancel_unknown:
            for order in exchange_open:
                if order.client_order_id in unknown_exchange:
                    self.adapter.cancel_order(order.exchange_order_id)
        else:
            for order in exchange_open:
                if order.client_order_id in unknown_exchange:
                    adopted = ManagedOrder(
                        intent_key=f"adopted-{order.client_order_id}",
                        order=OrderRequest(
                            client_order_id=order.client_order_id,
                            symbol=self.adapter.rules.symbol,
                            side="BUY",
                            price=Decimal("0"),
                            qty=Decimal("0"),
                            order_type="limit",
                            time_in_force="GTC",
                            post_only=False,
                        ),
                        status=order,
                    )
                    self.managed[adopted.intent_key] = adopted
                    self.by_client_id[order.client_order_id] = adopted
        return unknown_exchange, missing_local

    def open_order_count(self) -> int:
        return sum(1 for m in self.managed.values() if m.status.status in {"OPEN", "PARTIALLY_FILLED"})
