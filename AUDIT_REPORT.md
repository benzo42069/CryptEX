# CryptEX Full-System Validation Audit Report

## Summary of Issues Found

1. **Order replacement idempotency bug**: cancel/replace reused the same `client_order_id`, allowing adapters to return stale prior orders instead of creating a fresh replacement.
2. **Inventory drift bug**: post-fill accounting re-applied already-accounted fills on every tick, causing inventory/quote drift.
3. **Recovery gap**: restart reconciliation loaded config hash and last mid, but did not restore persisted positions.
4. **Failure-mode coverage gap**: no deterministic way to simulate transient `503 unknown status` exchange responses for retry validation.
5. **Cross-field config consistency gaps**: metrics tags could diverge from strategy identity/symbol without explicit hard-fail checks.

## Categorized Fixes

### Schema + Config
- Preserved strict schema validation flow and strengthened cross-field config checks in loader:
  - `ops.metrics.tags.mode == run_mode`
  - `ops.metrics.tags.symbol == market.symbol`
  - `ops.metrics.tags.strategy_id == strategy_id`
  - `base_order_notional_usd <= max_single_order_notional_usd`
- Validation remains fail-fast and actionable with explicit path+message errors.

### Execution + Order Lifecycle
- Fixed cancel/replace to issue a **new deterministic client order id per replace sequence** while preserving per-intent management.
- Kept duplicate-submission dedupe for same open intent.
- Preserved rate-limit gates on both new orders and cancels during replace operations.

### Exchange Adapter
- Added deterministic transient-failure injector in live adapter for controlled simulation of `503 unknown order status` and retry behavior.
- Hardened order update parsing to require valid IDs and reject malformed payloads.

### State + Recovery
- Restored persisted `positions.base` and `positions.quote` during startup reconciliation.
- Kept config-hash mismatch guard to prevent unsafe state reuse across different strategy resolutions.

### Risk + Runtime Safety
- Fixed post-fill accounting to process **delta-filled quantity only**, eliminating repeated inventory PnL drift.
- Existing stale data / websocket disconnect / circuit-breaker / open-order caps stay enforced before placement.

## Failure Modes Validated

1. websocket disconnect shutdown trigger
2. exchange transient 503 with retry success and retry exhaustion
3. duplicate order submission dedupe
4. cancel+replace with fresh client IDs
5. stale market data rejection
6. volatility circuit breaker trip
7. restart restore of persisted positions
8. invalid symbol/precision path rejection
9. insufficient balance error propagation
10. cancel rate-limit enforcement

## Remaining Risks

1. `LiveExchangeAdapter` remains an in-memory stub and is not yet a real Kraken REST/WS transport implementation.
2. Full exchange reconciliation for unknown/adopted orders is limited by missing side/price/qty fields in adapter `OrderStatus` model.
3. Account equity/PnL mark-to-market is simplified; drawdown checks are functional but not tied to real balance snapshots.

## Assumptions Made

1. Strategy precision fields (`price_precision`, `qty_precision`) are authoritative for the target instrument.
2. Default startup policy is to cancel unknown exchange orders unless explicitly adopting.
3. Grid runtime currently prioritizes safety and bounded placement throughput over full-level immediate deployment.
