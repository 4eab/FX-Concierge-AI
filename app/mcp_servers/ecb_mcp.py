"""
ECB MCP Server — exposes two tools to ADK agents:

  fetch_ecb_history(currency_pair: str)
      Pull the full ECB historical XML (eurofxref-hist.xml).
      Returns a list of {"date", "source_currency", "target_currency", "rate"} records.
      Slow (one HTTP call, ~400KB XML). Call once on setup per currency.

  fetch_ecb_daily(currency_pairs: list[str])
      Pull today's ECB reference rates (eurofxref.xml, ~16:00 CET EOD).
      Returns a list of {"date", "source_currency", "target_currency", "rate"} records.
      Fast. Used for the nightly EOD supplement task at 23:30 CST.

Run as stdio MCP server:
  python -m app.mcp_servers.ecb_mcp
"""

import asyncio
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx
import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

# ECB XML endpoints
ECB_HIST_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml"
ECB_DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

# XML namespaces
NS = {
    "gesmes": "http://www.gesmes.org/xml/2002-08-01",
    "ecb": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref",
}

app = Server("ecb-mcp")


def _parse_ecb_xml(content: bytes, target_currencies: list[str] | None = None) -> list[dict]:
    """Parse ECB Cube XML and return rate records.

    ECB rates are expressed as: 1 EUR = X <currency>
    So source_currency="EUR", target_currency=<currency>.
    """
    root = ET.fromstring(content)
    records = []

    for cube_time in root.findall(".//ecb:Cube[@time]", NS):
        rate_date = date.fromisoformat(cube_time.attrib["time"])

        for cube_rate in cube_time.findall("ecb:Cube", NS):
            currency = cube_rate.attrib.get("currency", "")
            rate_str = cube_rate.attrib.get("rate", "")

            if not currency or not rate_str:
                continue

            if target_currencies and currency not in target_currencies:
                continue

            try:
                rate = float(Decimal(rate_str))
            except InvalidOperation:
                continue

            records.append(
                {
                    "date": rate_date.isoformat(),
                    "source_currency": "EUR",
                    "target_currency": currency,
                    "rate": rate,
                    "rate_type": "mid",
                    "source": "ECB",
                }
            )

    return records


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="fetch_ecb_history",
            description=(
                "Fetch the full ECB historical exchange rate data (eurofxref-hist.xml). "
                "All rates are EUR-based: 1 EUR = X target_currency. "
                "Pass a single target_currency (e.g. 'CNY') to filter. "
                "Returns up to ~5000 date records. Slow — call once per currency setup."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target_currency": {
                        "type": "string",
                        "description": "3-letter ISO currency code to filter (e.g. 'CNY'). "
                        "If omitted, returns all ~30 currencies.",
                    }
                },
                "required": [],
            },
        ),
        types.Tool(
            name="fetch_ecb_daily",
            description=(
                "Fetch today's ECB reference rates (eurofxref.xml). "
                "Published around 16:00 CET — represents the current trading day's mid price. "
                "All rates are EUR-based: 1 EUR = X target_currency. "
                "Use for EOD historical supplement (nightly task)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target_currencies": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of 3-letter ISO codes to fetch (e.g. ['CNY', 'USD']). "
                        "If omitted, returns all available.",
                    }
                },
                "required": [],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    async with httpx.AsyncClient(timeout=60) as client:
        if name == "fetch_ecb_history":
            target_currency = arguments.get("target_currency")
            targets = [target_currency] if target_currency else None

            response = await client.get(ECB_HIST_URL)
            response.raise_for_status()
            records = _parse_ecb_xml(response.content, targets)

            import json
            return [types.TextContent(type="text", text=json.dumps(records))]

        elif name == "fetch_ecb_daily":
            target_currencies = arguments.get("target_currencies")
            targets = target_currencies if target_currencies else None

            response = await client.get(ECB_DAILY_URL)
            response.raise_for_status()
            records = _parse_ecb_xml(response.content, targets)

            import json
            return [types.TextContent(type="text", text=json.dumps(records))]

        else:
            raise ValueError(f"Unknown tool: {name}")


async def main() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
