"""
BOC (Bank of China) RateSource implementation.
"""

from datetime import datetime
from decimal import Decimal
import logging

import httpx
from bs4 import BeautifulSoup

from app.sources.base import RateQuote, RateSource

logger = logging.getLogger(__name__)


class BOCRateSource(RateSource):
    """Realtime rate source scraping Bank of China."""

    BOC_URL = "https://www.boc.cn/sourcedb/whpj/"
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.boc.cn/",
    }

    # Mapping: ISO 4217 -> Chinese name used in BOC data-currency attribute
    ISO_TO_BOC_NAME: dict[str, str] = {
        "EUR": "欧元",
        "USD": "美元",
        "GBP": "英镑",
        "JPY": "日元",
        "HKD": "港币",
        "AUD": "澳大利亚元",
        "CAD": "加拿大元",
        "CHF": "瑞士法郎",
        "SGD": "新加坡元",
        "NOK": "挪威克朗",
        "SEK": "瑞典克朗",
        "DKK": "丹麦克朗",
        "NZD": "新西兰元",
        "MYR": "马来西亚林吉特",
        "RUB": "卢布",
        "ZAR": "南非兰特",
        "KRW": "韩元",
        "THB": "泰国铢",
        "AED": "阿联酋迪拉姆",
        "SAR": "沙特里亚尔",
    }

    def supports(self, currency_from: str, currency_to: str) -> bool:
        c_from = currency_from.upper()
        c_to = currency_to.upper()
        
        # We support direct pair against CNY, inverted CNY pair, or cross rates between supported currencies
        is_from_supported = c_from in self.ISO_TO_BOC_NAME or c_from == "CNY"
        is_to_supported = c_to in self.ISO_TO_BOC_NAME or c_to == "CNY"
        
        # Don't support converting same currency
        if c_from == c_to:
            return False
            
        return is_from_supported and is_to_supported

    def _extract_spot_sell(self, soup: BeautifulSoup, iso_code: str) -> Decimal:
        """Extract spot sell rate for a given currency from BeautifulSoup."""
        if iso_code == "CNY":
            return Decimal("1.0")
            
        boc_name = self.ISO_TO_BOC_NAME.get(iso_code)
        if not boc_name:
            raise ValueError(f"Unsupported currency: {iso_code}")

        row = soup.find("tr", attrs={"data-currency": boc_name})
        if row is None:
            # Fallback search by checking text content of the first td
            for tr in soup.find_all("tr"):
                cells = tr.find_all("td")
                if cells and cells[0].text.strip() == boc_name:
                    row = tr
                    break

        if row is None:
            raise RuntimeError(f"Currency {iso_code} ({boc_name}) not found on BOC page")

        cells = row.find_all("td")
        if len(cells) < 7:
            raise RuntimeError("BOC page structure changed, unexpected cell count")

        # Column layout: 货币名称 | 现汇买入价 | 现钞买入价 | 现汇卖出价 | 现钞卖出价 | 中行折算价 | 发布日期
        spot_sell_str = cells[3].text.strip()
        if not spot_sell_str:
            raise RuntimeError(f"BOC spot sell rate is empty for {iso_code}")

        # BOC quotes per 100 units
        return Decimal(spot_sell_str) / Decimal("100")

    async def fetch(self, currency_from: str, currency_to: str) -> RateQuote:
        if not self.supports(currency_from, currency_to):
            raise ValueError(
                f"BOCRateSource does not support pair {currency_from}/{currency_to}"
            )

        c_from = currency_from.upper()
        c_to = currency_to.upper()

        async with httpx.AsyncClient(timeout=20, headers=self.HEADERS) as client:
            response = await client.get(self.BOC_URL)
            response.raise_for_status()
            response.encoding = "utf-8"

        soup = BeautifulSoup(response.text, "html.parser")
        
        # Extract individual rates relative to CNY
        rate_from = self._extract_spot_sell(soup, c_from)
        rate_to = self._extract_spot_sell(soup, c_to)
        
        # Cross rate: rate_from CNY per unit of c_from, rate_to CNY per unit of c_to.
        # So to buy 1 unit of c_to, it costs (rate_to / rate_from) units of c_from.
        # But wait! A RateQuote rate shows how many c_to units you get per 1 c_from.
        # So rate(c_from -> c_to) is rate_from / rate_to.
        # E.g. EUR/CNY = 7.85, USD/CNY = 7.25. EUR/USD = 7.85 / 7.25 = 1.0827 (USD per EUR).
        rate = rate_from / rate_to

        # Find publication date from one of the non-CNY rows
        date_currency = c_from if c_from != "CNY" else c_to
        boc_name = self.ISO_TO_BOC_NAME[date_currency]
        
        row = soup.find("tr", attrs={"data-currency": boc_name})
        if row is None:
            for tr in soup.find_all("tr"):
                cells = tr.find_all("td")
                if cells and cells[0].text.strip() == boc_name:
                    row = tr
                    break

        if row is not None:
            cells = row.find_all("td")
            published_str = cells[6].text.strip()
        else:
            published_str = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        
        from datetime import timezone as timezone_cls, timedelta
        beijing_tz = timezone_cls(timedelta(hours=8))
        try:
            published_at = datetime.strptime(published_str, "%Y/%m/%d %H:%M:%S").replace(tzinfo=beijing_tz)
        except Exception:
            published_at = datetime.now(beijing_tz)

        return RateQuote(
            currency_from=c_from,
            currency_to=c_to,
            rate=rate,
            timestamp=published_at,
            source="BOC_CROSS" if (c_from != "CNY" and c_to != "CNY") else "BOC",
            rate_type="spot_sell",
        )
