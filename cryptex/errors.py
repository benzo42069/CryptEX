class ConfigError(Exception):
    """Raised when strategy or environment configuration is invalid."""


class RiskViolation(Exception):
    """Raised when a risk guard trips."""


class ExchangeValidationError(Exception):
    """Raised when an order is invalid for exchange rules."""


class ExchangeTransientError(Exception):
    """Raised when exchange status is unknown/transient and call should be retried."""


class RateLimitExceededError(Exception):
    """Raised when exchange rate limits prevent immediate order action."""


class InsufficientBalanceError(Exception):
    """Raised when account balances are insufficient for requested action."""


class MarketDataStaleError(Exception):
    """Raised when market data is stale beyond tolerance."""


class WebsocketDisconnectError(Exception):
    """Raised when websocket disconnect exceeds grace period."""
