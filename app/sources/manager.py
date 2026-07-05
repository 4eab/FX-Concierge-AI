"""
Source manager that coordinates multiple RateSources.
"""

from app.sources.base import RateQuote, RateSource
from app.sources.boc import BOCRateSource
from app.sources.ecb import ECBRateSource


class RateSourceManager:
    """Manages list of rate sources and determines the best source for a given pair."""

    def __init__(self) -> None:
        self.sources: list[RateSource] = [
            BOCRateSource(),
            ECBRateSource(),
        ]

    def get_source_for(self, currency_from: str, currency_to: str) -> RateSource | None:
        """Find the first rate source that supports the given currency pair."""
        for source in self.sources:
            if source.supports(currency_from, currency_to):
                return source
        return None

    async def fetch_rate(self, currency_from: str, currency_to: str) -> RateQuote:
        """Fetch rate using the best available source."""
        source = self.get_source_for(currency_from, currency_to)
        if not source:
            raise ValueError(
                f"No rate source supports pair {currency_from}/{currency_to}"
            )
        return await source.fetch(currency_from, currency_to)


# Singleton instance
rate_manager = RateSourceManager()
