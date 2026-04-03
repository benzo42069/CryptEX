from __future__ import annotations

import time
from dataclasses import dataclass

from .errors import MarketDataStaleError, WebsocketDisconnectError


@dataclass
class WsHealth:
    connected: bool = False
    last_msg_at: float = 0.0
    last_ping_at: float = 0.0
    disconnect_started_at: float | None = None


class ReliableWebsocket:
    def __init__(self, stale_after_sec: int, disconnect_grace_sec: int, base_backoff_sec: float = 0.5, max_backoff_sec: float = 8.0) -> None:
        self.stale_after_sec = stale_after_sec
        self.disconnect_grace_sec = disconnect_grace_sec
        self.base_backoff_sec = base_backoff_sec
        self.max_backoff_sec = max_backoff_sec
        self.health = WsHealth()
        self.reconnect_attempts = 0

    def on_connect(self) -> None:
        now = time.time()
        self.health.connected = True
        self.health.last_msg_at = now
        self.health.last_ping_at = now
        self.health.disconnect_started_at = None
        self.reconnect_attempts = 0

    def on_message(self) -> None:
        self.health.last_msg_at = time.time()

    def on_ping(self) -> None:
        self.health.last_ping_at = time.time()

    def on_disconnect(self) -> None:
        now = time.time()
        self.health.connected = False
        if self.health.disconnect_started_at is None:
            self.health.disconnect_started_at = now

    def mark_reconnect_attempt(self) -> float:
        self.on_disconnect()
        return self.next_backoff()

    def next_backoff(self) -> float:
        backoff = min(self.max_backoff_sec, self.base_backoff_sec * (2 ** self.reconnect_attempts))
        self.reconnect_attempts += 1
        return backoff

    def should_force_shutdown(self) -> bool:
        now = time.time()
        if not self.health.connected and self.health.disconnect_started_at is not None:
            return now - self.health.disconnect_started_at > self.disconnect_grace_sec
        return False

    def assert_healthy(self) -> None:
        now = time.time()
        if now - self.health.last_msg_at > self.stale_after_sec:
            raise MarketDataStaleError("stale market data")
        if self.health.connected and now - self.health.last_ping_at > self.stale_after_sec:
            raise MarketDataStaleError("websocket heartbeat stale")
        if self.should_force_shutdown():
            raise WebsocketDisconnectError("websocket disconnect grace exceeded")
