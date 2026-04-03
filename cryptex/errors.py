class ConfigError(Exception):
    """Raised when strategy or environment configuration is invalid."""


class RiskViolation(Exception):
    """Raised when a risk guard trips."""


class ExchangeValidationError(Exception):
    """Raised when an order is invalid for exchange rules."""


class MarketDataStaleError(Exception):
    """Raised when market data is stale beyond tolerance."""


class WebsocketDisconnectError(Exception):
    """Raised when websocket disconnect exceeds grace period."""
