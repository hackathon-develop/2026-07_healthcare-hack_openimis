"""
Smoke test for the openIMIS MCP server.

Connects as an MCP client over streamable HTTP and calls each tool with
a known-good test value. Run the server first (`uv run server.py`), then:

    uv run test_server.py --chf-id SOME_TEST_CHF_ID

If you don't know a valid CHF ID yet, run with no args first — it will
list a few insurees for you to pick a real one from.
"""

import argparse
import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

SERVER_URL = "http://localhost:8080/mcp"


async def call_tool(session: ClientSession, name: str, arguments: dict):
    print(f"\n--- {name}({arguments}) ---")
    result = await session.call_tool(name, arguments)
    for item in result.content:
        if hasattr(item, "text"):
            try:
                print(json.dumps(json.loads(item.text), indent=2)[:2000])
            except (json.JSONDecodeError, TypeError):
                print(item.text[:2000])


async def main(chf_id: str | None):
    async with streamablehttp_client(SERVER_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("Available tools:", [t.name for t in tools.tools])

            if not chf_id:
                # No test CHF ID given — list some insurees so the caller
                # can pick a real one and re-run with --chf-id.
                await call_tool(session, "search_insuree", {"last_name": "a"})
                print("\nRe-run with --chf-id <one of the above> to exercise the other tools.")
                return

            await call_tool(session, "search_insuree", {"chf_id": chf_id})
            await call_tool(session, "get_active_policies", {"chf_id": chf_id})
            await call_tool(session, "get_claims_for_insuree", {
                "chf_id": chf_id,
                "start_date": "2020-01-01",
                "end_date": "2026-12-31",
            })
            await call_tool(session, "list_health_facilities", {})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--chf-id", default=None, help="A known-valid CHF ID from your test data")
    args = parser.parse_args()
    asyncio.run(main(args.chf_id))
