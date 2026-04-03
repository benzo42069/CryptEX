from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from typing import Literal

from .errors import ExchangeValidationError

OrderSide = Literal["BUY", "SELL"]


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
        return symbol.replace("/", "").upper()

    def quantize_price(self, price: Decimal) -> Decimal:
        return (price / self.rules.tick_size).to_integral_value(rounding=ROUND_DOWN) * self.rules.tick_size

    def quantize_qty(self, qty: Decimal) -> Decimal:
        return (qty / self.rules.lot_size).to_integral_value(rounding=ROUND_DOWN) * self.rules.lot_size

    def validate_order(self, req: OrderRequest) -> OrderRequest:
        if self.normalize_symbol(req.symbol) != self.normalize_symbol(self.rules.symbol):
            raise ExchangeValidationError(f"symbol mismatch: got {req.symbol}, expected {self.rules.symbol}")

        req.price = self.quantize_price(req.price)
        req.qty = self.quantize_qty(req.qty)

        if req.qty < self.rules.min_qty:
            raise ExchangeValidationError(f"qty {req.qty} below min_qty {self.rules.min_qty}")
        notional = req.price * req.qty
        if notional < self.rules.min_notional:
            raise ExchangeValidationError(f"notional {notional} below min_notional {self.rules.min_notional}")

        if req.post_only and req.time_in_force not in {"GTC"}:
            raise ExchangeValidationError("post_only requires GTC")

        return req


class LiveExchangeAdapter(BaseExchangeAdapter):
    def __init__(self, rules: ExchangeRules) -> None:
        super().__init__(rules)
        self._orders: dict[str, OrderStatus] = {}
        self._order_seq = 0

    def place_order(self, req: OrderRequest) -> OrderStatus:
        req = self.validate_order(req)
        self._order_seq += 1
        ex_id = f"L-{self._order_seq}"
        st = OrderStatus(exchange_order_id=ex_id, client_order_id=req.client_order_id, status="OPEN")
        self._orders[ex_id] = st
        return st

    def cancel_order(self, exchange_order_id: str) -> OrderStatus:
        st = self._orders[exchange_order_id]
        if st.status in {"FILLED", "CANCELED"}:
            return st
        st.status = "CANCELED"
        st.updated_at = time.time()
        return st

    def fetch_open_orders(self) -> list[OrderStatus]:
        return [o for o in self._orders.values() if o.status == "OPEN"]

    def parse_order_update(self, payload: dict) -> OrderStatus:
        return OrderStatus(
            exchange_order_id=str(payload["exchange_order_id"]),
            client_order_id=payload["client_order_id"],
            status=payload["status"],
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
        self._order_seq = 0

    def place_order(self, req: OrderRequest, market_mid: Decimal) -> OrderStatus:
        req = self.validate_order(req)
        self._order_seq += 1
        ex_id = f"P-{self._order_seq}"

        aggressive = (req.side == "BUY" and req.price >= market_mid) or (req.side == "SELL" and req.price <= market_mid)
        if aggressive:
            slip = (self.slippage_bps / Decimal("10000")) * market_mid
            fill_price = market_mid + (slip if req.side == "BUY" else -slip)
            fee_bps = self.taker_fee_bps
            status = "FILLED"
            filled = req.qty
        else:
            fill_price = Decimal("0")
            fee_bps = self.maker_fee_bps
            status = "OPEN"
            filled = Decimal("0")

        fee = (filled * (fill_price or req.price)) * fee_bps / Decimal("10000")
        st = OrderStatus(
            exchange_order_id=ex_id,
            client_order_id=req.client_order_id,
            status=status,
            filled_qty=filled,
            avg_fill_price=fill_price,
            fee=fee,
        )
        self._orders[ex_id] = st
        return st

    def fetch_open_orders(self) -> list[OrderStatus]:
        return [o for o in self._orders.values() if o.status == "OPEN"]

    def cancel_order(self, exchange_order_id: str) -> OrderStatus:
        st = self._orders[exchange_order_id]
        if st.status == "OPEN":
            st.status = "CANCELED"
        return st
