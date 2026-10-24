"""Exercise the unified image's MCP transport and its stock symbol library."""
import asyncio
import importlib.metadata
import json
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    version = importlib.metadata.version("mcp-server-kicad")
    assert version == "0.21.1", version
    expected_kicad = os.environ["KICAD_VERSION"]
    params = StdioServerParameters(command="/usr/local/bin/kicad-mcp")
    async with asyncio.timeout(60):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                names = {tool.name for tool in tools}
                required = {"get_version", "get_symbol_info", "run_erc", "run_drc", "fill_zones", "autoroute_pcb"}
                assert required <= names, sorted(required - names)
                result = await session.call_tool("get_version", {})
                text = "\n".join(item.text for item in result.content if item.type == "text")
                assert not result.is_error and expected_kicad in text, text
                result = await session.call_tool("get_symbol_info", {
                    "symbol_name": "R",
                    "symbol_lib_path": "/usr/share/kicad/symbols/Device.kicad_sym",
                })
                text = "\n".join(item.text for item in result.content if item.type == "text")
                assert not result.is_error and "Symbol: R" in text and "Pin 1:" in text, text
    print(json.dumps({"mcp_version": version, "kicad_version": expected_kicad, "tools": len(tools), "stock_symbol_read": True}))


if __name__ == "__main__":
    asyncio.run(main())
