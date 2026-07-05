"""
ECB (European Central Bank) RateSource implementation.
Supports direct EUR pairs and cross-rates (triangular conversion) via ECB daily API.
"""

import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import logging

import httpx

from app.sources.base import RateQuote, RateSource

logger = logging.getLogger(__name__)


class ECBRateSource(RateSource):
    """EOD Rate source using the European Central Bank API."""

    ECB_DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
    NS = {
        "gesmes": "http://www.gesmes.org/xml/2002-08-01",
        "ecb": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref",
    }

    # Set of currencies known to be supported by ECB
    ECB_CURRENCIES = {
        "USD", "JPY", "BGN", "CZK", "DKK", "GBP", "HUF", "PLN", "RON",
        "SEK", "CHF", "ISK", "NOK", "TRY", "AUD", "BRL", "CAD", "CNY",
        "HKD", "IDR", "ILS", "INR", "KRW", "MXN", "MYR", "NZD", "PHP",
        "SGD", "THB", "ZAR",
    }

    def supports(self, currency_from: str, currency_to: str) -> bool:
        c_from = currency_from.upper()
        c_to = currency_to.upper()

        # Direct EUR base: 1 EUR = X currency
        if c_from == "EUR" and c_to in self.ECB_CURRENCIES:
            return True
        # Inverted: 1 currency = X EUR
        if c_to == "EUR" and c_from in self.ECB_CURRENCIES:
            return True
        # Cross rate: 1 currency_A = X currency_B (computed via EUR)
        if c_from in self.ECB_CURRENCIES and c_to in self.ECB_CURRENCIES:
            return True

        return False

    async def fetch_ecb_raw_rates(self) -> tuple[datetime, dict[str, Decimal]]:
        """Fetch all rates from ECB daily XML. Returns timestamp and rate map (EUR base)."""
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(self.ECB_DAILY_URL)
            response.raise_for_status()

        root = ET.fromstring(response.content)
        rates: dict[str, Decimal] = {}
        pub_date_str = None

        cube_time = root.find(".//ecb:Cube[@time]", self.NS)
        if cube_time is not None:
            pub_date_str = cube_time.attrib["time"]
            for cube_rate in cube_time.findall("ecb:Cube", self.NS):
                currency = cube_rate.attrib.get("currency", "")
                rate_str = cube_rate.attrib.get("rate", "")
                if currency and rate_str:
                    try:
                        rates[currency] = Decimal(rate_str)
                    except InvalidOperation:
                        pass

        # Parse publication date to datetime (16:00 CET is standard publication time)
        if pub_date_str:
            pub_date = date.fromisoformat(pub_date_str)
            # Combine with 16:00 CET/CEST (approx UTC+1)
            pub_time = datetime.combine(pub_date, datetime.min.time()).replace(tzinfo=timezone.utc)
        else:
            pub_time = datetime.now(timezone.utc)

        return pub_time, rates

    async def fetch(self, currency_from: str, currency_to: str) -> RateQuote:
        if not self.supports(currency_from, currency_to):
            raise ValueError(
                f"ECBRateSource does not support pair {currency_from}/{currency_to}"
            )

        c_from = currency_from.upper()
        c_to = currency_to.upper()

        timestamp, eur_rates = await self.fetch_ecb_raw_rates()

        if c_from == "EUR":
            # 1 EUR = X currency_to
            rate = eur_rates[c_to]
        elif c_to == "EUR":
            # 1 currency_from = 1 / X EUR
            rate = Decimal("1") / eur_rates[c_from]
        else:
            # Triangular: 1 currency_from = X currency_to
            # rate(EUR->c_to) / rate(EUR->c_from)
            rate = eur_rates[c_to] / eur_rates[c_from]

        return RateQuote(
            currency_from=c_from,
            currency_to=c_to,
            rate=rate,
            timestamp=timestamp,
            source="ECB",
            rate_type="mid",
        )
