"""
Unit tests for RateSource plugins and manager.
"""

from decimal import Decimal
import pytest

from app.sources.boc import BOCRateSource
from app.sources.ecb import ECBRateSource
from app.sources.manager import RateSourceManager


@pytest.mark.asyncio
async def test_boc_supports() -> None:
    source = BOCRateSource()
    assert source.supports("EUR", "CNY") is True
    assert source.supports("USD", "CNY") is True
    # BOC now supports cross-rates and inverted pairs via CNY bridge calculation
    assert source.supports("EUR", "USD") is True
    assert source.supports("CNY", "EUR") is True


@pytest.mark.asyncio
async def test_ecb_supports() -> None:
    source = ECBRateSource()
    assert source.supports("EUR", "CNY") is True
    assert source.supports("EUR", "USD") is True
    # Inverted
    assert source.supports("CNY", "EUR") is True
    # Cross rate
    assert source.supports("USD", "CNY") is True
    # Unsupported
    assert source.supports("USD", "XYZ") is False


@pytest.mark.asyncio
async def test_manager_routing() -> None:
    manager = RateSourceManager()
    
    # EUR/CNY is supported by both, should prefer BOC because it's first in manager's list
    boc_source = manager.get_source_for("EUR", "CNY")
    assert isinstance(boc_source, BOCRateSource)
    
    # EUR/USD is now supported by both, and should prefer BOC because it's first in manager's list
    ecb_source = manager.get_source_for("EUR", "USD")
    assert isinstance(ecb_source, BOCRateSource)

    # Completely unsupported pair
    assert manager.get_source_for("USD", "XYZ") is None
