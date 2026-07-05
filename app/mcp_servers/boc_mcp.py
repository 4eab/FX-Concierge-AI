"""
BOC (Bank of China) MCP Server — exposes real-time FX rates scraped from BOC.

  fetch_boc_rate(target_currency: str)
      Scrape the BOC FX rate page for a specific currency.
      Returns the spot sell (现汇卖出价) which is what retail buyers pay.
      Source: https://www.boc.cn/sourcedb/whpj/

Currency name mapping (Chinese → ISO):
  The BOC page uses Chinese currency names in data-currency attributes.
  This server handles the mapping internally.

Run as stdio MCP server:
  python -m app.mcp_servers.boc_mcp
"""

import asyncio
import json
from datetime import datetime

import httpx
import mcp.server.stdio
import mcp.types as types
from bs4 import BeautifulSoup
from mcp.server import Server

BOC_URL = "https://www.boc.cn/sourcedb/whpj/"
BOC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.boc.cn/",
}

from app.sources.boc import BOCRateSource

ISO_TO_BOC_NAME = BOCRateSource.ISO_TO_BOC_NAME

app = Server("boc-mcp")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    supported = ", ".join(sorted(ISO_TO_BOC_NAME.keys()))
    return [
        types.Tool(
            name="fetch_boc_rate",
            description=(
                "Fetch real-time FX spot sell rate from Bank of China (BOC). "
                "Returns the price in CNY you pay to buy 1 unit of the foreign currency. "
                f"Supported currencies: {supported}. "
                "Published throughout trading hours. Use for intraday scoring (09:00/15:30/21:00 CST)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target_currency": {
                        "type": "string",
                        "description": "ISO 4217 code of the currency to buy (e.g. 'EUR', 'USD').",
                    }
                },
                "required": ["target_currency"],
            },
        ),
        types.Tool(
            name="list_boc_supported_currencies",
            description="List all currencies supported by the BOC rate source.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "list_boc_supported_currencies":
        result = {iso: cn for iso, cn in ISO_TO_BOC_NAME.items()}
        return [types.TextContent(type="text", text=json.dumps(result))]

    if name != "fetch_boc_rate":
        raise ValueError(f"Unknown tool: {name}")

    iso_code = arguments["target_currency"].upper()

    if iso_code not in ISO_TO_BOC_NAME:
        return [
            types.TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": f"Currency '{iso_code}' is not supported by BOC source. "
                        f"Supported: {sorted(ISO_TO_BOC_NAME.keys())}"
                    }
                ),
            )
        ]

    try:
        source = BOCRateSource()
        quote = await source.fetch(iso_code, "CNY")
        result = {
            "source_currency": quote.currency_from,
            "target_currency": quote.currency_to,
            "spot_sell": float(quote.rate),
            "spot_buy": None,
            "rate": float(quote.rate),
            "rate_type": quote.rate_type,
            "published_at": quote.timestamp.isoformat(),
            "source": quote.source,
        }
    except Exception as e:
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]

    return [types.TextContent(type="text", text=json.dumps(result))]


async def main() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
