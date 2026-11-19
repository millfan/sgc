"""Market data providers behind one pluggable interface."""

from stockassistant.data.provider import MarketDataProvider, make_provider

__all__ = ["MarketDataProvider", "make_provider"]
