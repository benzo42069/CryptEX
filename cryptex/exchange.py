from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from typing import Literal

from .errors import (
    ExchangeTransientError,
    ExchangeValidationError,
    InsufficientBalanceError,
    RateLimitExceededError,
)

OrderSide = Literal["BUY", "SELL"]
VALID_TIF = {"GTC", "IOC", "FOK"}
VALID_STATUS = {"NEW", "OPEN", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED"}


@dataclass(frozen=True)
class ExchangeRules:
    symbol: str
    tick_size: Decimal
    lot_size: Decimal
    min_qty: Decimal
    min_notional: Decimal


@dataclass
class OrderRequest:
    client_order_id: str
    symbol: str
    side: OrderSide
    price: Decimal
    qty: Decimal
    order_type: str
    time_in_force: str
    post_only: bool
    reduce_only: bool = False


@dataclass
class OrderStatus:
    exchange_order_id: str
    client_order_id: str
    status: str
    filled_qty: Decimal = Decimal("0")
    avg_fill_price: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    updated_at: float = field(default_factory=time.time)


class BaseExchangeAdapter:
    def __init__(self, rules: ExchangeRules) -> None:
        self.rules = rules

    def normalize_symbol(self, symbol: str) -> str:
        return symbol.replace("/", "").replace("-", "").upper()

    def quantize_price(self, price: Decimal) -> Decimal:
        return (price / self.rules.tick_size).to_integral_value(rounding=ROUND_DOWN) * self.rules.tick_size

    def quantize_qty(self, qty: Decimal) -> Decimal:
        return (qty / self.rules.lot_size).to_integral_value(rounding=ROUND_DOWN) * self.rules.lot_size

    def normalize_request(self, req: OrderRequest) -> OrderRequest:
        req.price = self.quantize_price(req.price)
        req.qty = self.quantize_qty(req.qty)
        return req

    def validate_order(self, req: OrderRequest) -> OrderRequest:
        if self.normalize_symbol(req.symbol) != self.normalize_symbol(self.rules.symbol):
            raise ExchangeValidationError(f"symbol mismatch: got {req.symbol}, expected {self.rules.symbol}")
        if req.order_type != "limit":
            raise ExchangeValidationError("only limit orders are supported")
        if req.time_in_force not in VALID_TIF:
            raise ExchangeValidationError(f"unsupported time_in_force: {req.time_in_force}")
        if req.reduce_only:
            raise ExchangeValidationError("reduce_only is unsupported for spot market")

        req = self.normalize_request(req)
        if req.qty <= 0:
            raise ExchangeValidationError("qty must be > 0 after lot-size rounding")
        if req.price <= 0:
            raise ExchangeValidationError("price must be > 0 after tick-size rounding")

        if req.qty < self.rules.min_qty:
            raise ExchangeValidationError(f"qty {req.qty} below min_qty {self.rules.min_qty}")
        notional = req.price * req.qty
        if notional < self.rules.min_notional:
            raise ExchangeValidationError(f"notional {notional} below min_notional {self.rules.min_notional}")
        if req.post_only and req.time_in_force != "GTC":
            raise ExchangeValidationError("post_only requires GTC")
        return req


class LiveExchangeAdapter(BaseExchangeAdapter):
    def __init__(self, rules: ExchangeRules) -> None:
        super().__init__(rules)
        self._orders: dict[str, OrderStatus] = {}
        self._by_client: dict[str, str] = {}
        self._order_seq = 0
        self._transient_failures_remaining = 0

    def inject_transient_failures(self, count: int) -> None:
        self._transient_failures_remaining = max(0, count)

    def place_order(self, req: OrderRequest) -> OrderStatus:
        req = self.validate_order(req)
        if self._transient_failures_remaining > 0:
            self._transient_failures_remaining -= 1
            raise ExchangeTransientError("503 unknown order status")
        existing_id = self._by_client.get(req.client_order_id)
        if existing_id:
            return self._orders[existing_id]

        self._order_seq += 1
        ex_id = f"L-{self._order_seq}"
        st = OrderStatus(exchange_order_id=ex_id, client_order_id=req.client_order_id, status="OPEN")
        self._orders[ex_id] = st
        self._by_client[req.client_order_id] = ex_id
        return st

    def cancel_order(self, exchange_order_id: str) -> OrderStatus:
        if exchange_order_id not in self._orders:
            raise ExchangeValidationError(f"unknown exchange_order_id: {exchange_order_id}")
        st = self._orders[exchange_order_id]
        if st.status in {"FILLED", "CANCELED"}:
            return st
        st.status = "CANCELED"
        st.updated_at = time.time()
        return st

    def fetch_open_orders(self) -> list[OrderStatus]:
        return [o for o in self._orders.values() if o.status in {"OPEN", "PARTIALLY_FILLED"}]

    def parse_order_update(self, payload: dict) -> OrderStatus:
        status = str(payload["status"]).upper()
        if status not in VALID_STATUS:
            raise ExchangeValidationError(f"unknown order status '{status}'")
        exchange_order_id = str(payload["exchange_order_id"])
        client_order_id = str(payload["client_order_id"])
        if not exchange_order_id or not client_order_id:
            raise ExchangeValidationError("order update missing IDs")
        return OrderStatus(
            exchange_order_id=exchange_order_id,
            client_order_id=client_order_id,
            status=status,
            filled_qty=Decimal(str(payload.get("filled_qty", "0"))),
            avg_fill_price=Decimal(str(payload.get("avg_fill_price", "0"))),
            fee=Decimal(str(payload.get("fee", "0"))),
        )


class PaperExchangeAdapter(BaseExchangeAdapter):
    def __init__(self, rules: ExchangeRules, maker_fee_bps: float, taker_fee_bps: float, slippage_bps: float) -> None:
        super().__init__(rules)
        self.maker_fee_bps = Decimal(str(maker_fee_bps))
        self.taker_fee_bps = Decimal(str(taker_fee_bps))
        self.slippage_bps = Decimal(str(slippage_bps))
        self._orders: dict[str, OrderStatus] = {}
        self._by_client: dict[str, str] = {}
        self._order_seq = 0

    def place_order(self, req: OrderRequest, market_mid: Decimal) -> OrderStatus:
        req = self.validate_order(req)
        existing_id = self._by_client.get(req.client_order_id)
        if existing_id:
            return self._orders[existing_id]
        if req.qty * req.price > Decimal("500000"):
            raise InsufficientBalanceError("paper account balance exceeded")

        self._order_seq += 1
        ex_id = f"P-{self._order_seq}"

        aggressive = (req.side == "BUY" and req.price >= market_mid) or (req.side == "SELL" and req.price <= market_mid)
        near_touch = abs(req.price - market_mid) <= market_mid * Decimal("0.0005")
        if aggressive:
            fill_ratio = Decimal("1")
            fee_bps = self.taker_fee_bps
            fill_price = market_mid + ((self.slippage_bps / Decimal("10000")) * market_mid if req.side == "BUY" else -(self.slippage_bps / Decimal("10000")) * market_mid)
            status = "FILLED"
        elif near_touch:
            fill_ratio = Decimal("0.4")
            fee_bps = self.maker_fee_bps
            fill_price = req.price
            status = "PARTIALLY_FILLED"
        else:
            fill_ratio = Decimal("0")
            fee_bps = self.maker_fee_bps
            fill_price = req.price
            status = "OPEN"

        filled = (req.qty * fill_ratio).quantize(self.rules.lot_size)
        fee = (filled * fill_price) * fee_bps / Decimal("10000")
        st = OrderStatus(
            exchange_order_id=ex_id,
            client_order_id=req.client_order_id,
            status=status,
            filled_qty=filled,
            avg_fill_price=fill_price if filled > 0 else Decimal("0"),
            fee=fee,
        )
        self._orders[ex_id] = st
        self._by_client[req.client_order_id] = ex_id
        return st

    def fetch_open_orders(self) -> list[OrderStatus]:
        return [o for o in self._orders.values() if o.status in {"OPEN", "PARTIALLY_FILLED"}]

    def cancel_order(self, exchange_order_id: str) -> OrderStatus:
        if exchange_order_id not in self._orders:
            raise ExchangeValidationError(f"unknown exchange_order_id: {exchange_order_id}")
        st = self._orders[exchange_order_id]
        if st.status in {"OPEN", "PARTIALLY_FILLED"}:
            st.status = "CANCELED"
            st.updated_at = time.time()
        return st
