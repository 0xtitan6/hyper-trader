"""Custom exception hierarchy for hyper-trader.

Using these instead of bare exceptions makes failure modes explicit and
lets callers catch only what they intend to handle.
"""


class HyperTraderError(Exception):
    """Base class for all hyper-trader errors."""


class ConfigError(HyperTraderError):
    """Configuration is invalid or missing required values."""


class PreflightError(HyperTraderError):
    """Preflight checks failed; bot is not safe to start."""


class MarketMetaError(HyperTraderError):
    """Failed to load market metadata or look up a coin."""


class OrderError(HyperTraderError):
    """Order submission failed at the exchange layer."""
