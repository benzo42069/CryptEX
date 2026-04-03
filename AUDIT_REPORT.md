# CryptEX Full-System Validation Audit Report

## Summary of Issues Found

1. Execution schema allowed `execution.retry` to be omitted while runtime assumed the field existed.
2. Websocket health checks did not enforce heartbeat freshness, only message timestamps.
3. Restart flow persisted open orders but did not restore managed order state before reconciliation.
4. Runtime order placement path could raise transient/validation/rate-limit failures in-line without converting them into controlled risk counters.
5. Equity-based risk checks were using static day-start equity instead of mark-to-market inventory exposure.
6. Exchange cancel path accepted unknown exchange order IDs implicitly.

## Categorized Fixes

### Schema
- Strengthened strategy schema by making `execution.retry` required so runtime and config contract are consistent.

### Execution
- Added safe order submission wrapper that captures expected placement failures (`validation`, `rate-limit`, `insufficient balance`, transient exchange status), increments reject/action counters, and prevents uncontrolled runtime spirals.
- Added mark-to-market equity estimation (`start_equity + inventory_quote + inventory_base * mid`) and used it in both pre-order and post-fill risk checks.
- Extended checkpoint payload to persist full managed order state for restart-safe recovery.

### Exchange Adapter
- Hardened exchange validation after quantization (price/qty must remain positive).
- Added explicit rejection for cancel requests with unknown `exchange_order_id`.

### Risk
- Improved practical drawdown enforcement by using mark-to-market equity during checks.
- Preserved existing spread, inventory imbalance, reject ratio, cancel-fail ratio, and circuit-breaker controls.

### Runtime / Recovery
- Added managed-order restoration from persisted state before startup reconciliation.
- Added websocket heartbeat staleness detection to complement stale message detection and disconnect grace shutdown.

## Failure Modes Validated

1. websocket disconnect mid-trading -> graceful shutdown trigger
2. exchange 503 unknown status -> retry success and retry exhaustion paths
3. partial fill then cancel -> no double accounting of fills
4. rate limit exceeded -> explicit exception path and controlled handling in placement loop
5. duplicate order submission -> deduplicated by intent key
6. engine restart with persisted open orders -> managed state restored and reconciled
7. stale market data -> hard stop
8. heartbeat stale -> hard stop
9. invalid precision/symbol request -> exchange validation rejection
10. insufficient balance -> explicit propagation and accounting

## Remaining Risks

1. Live adapter is still a deterministic in-memory transport stub; production exchange REST/WS protocol specifics (auth, signing, sequencing) are outside current repository scope.
2. Inventory imbalance metric is currently passed as `0` in engine loop; strategy-specific imbalance formula should be added when portfolio accounting model is expanded.
3. Reconciliation adopts/cancels based on currently available order snapshot data; richer exchange fields would improve adoption fidelity.

## Assumptions Made

1. Strategy precision fields are authoritative for effective tick/lot construction in this codebase.
2. Startup reconciliation default (`cancel_unknown=True`) is the safest behavior for unattended live recovery.
3. Existing architecture intentionally uses bounded per-cycle grid placement to limit order bursts and exchange throttle pressure.
