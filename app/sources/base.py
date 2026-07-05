"""
RateSource base interface and RateQuote definition.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass
class RateQuote:
    currency_from: str  # e.g., "EUR" (base currency)
    currency_to: str    # e.g., "CNY" (quote currency)
    rate: Decimal       # 1 currency_from = X currency_to
    timestamp: datetime
    source: str         # "BOC" or "ECB"
    rate_type: str = "mid"  # "spot_sell", "spot_buy", "mid"


class RateSource(ABC):
    """Abstract Base Class representing a realtime or EOD currency rate source."""

    @abstractmethod
    async def fetch(self, currency_from: str, currency_to: str) -> RateQuote:
        """Fetch the rate between currency_from and currency_to."""
        pass

    @abstractmethod
    def supports(self, currency_from: str, currency_to: str) -> bool:
        """Return True if this source supports the given currency pair."""
        pass
