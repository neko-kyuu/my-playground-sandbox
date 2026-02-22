from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional


def _json_dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _import_mcp():
    """
    MCP Python SDK imports changed a bit across versions; use tolerant imports.
    """
    try:
        from mcp import ClientSession, StdioServerParameters, types  # type: ignore
        from mcp.client.stdio import stdio_client  # type: ignore
        from mcp.types import AnyUrl  # type: ignore

        return ClientSession, StdioServerParameters, types, stdio_client, AnyUrl
    except Exception:
        from mcp.client.session import ClientSession  # type: ignore
        from mcp.client.stdio import StdioServerParameters, stdio_client  # type: ignore
        import mcp.types as types  # type: ignore
        from mcp.types import AnyUrl  # type: ignore

        return ClientSession, StdioServerParameters, types, stdio_client, AnyUrl


async def _run(
    config_path: str,
    list_only: bool,
    stats: bool,
    search: Optional[str],
    generate: Optional[str],
) -> None:
    ClientSession, StdioServerParameters, types, stdio_client, AnyUrl = _import_mcp()

    here = Path(__file__).resolve().parent
    server_py = str((here / "server.py").resolve())
    cfg = str(Path(config_path).expanduser().resolve())

    server_params = StdioServerParameters(
        command=sys.executable,
        args=[server_py, "--config", cfg],
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            resources = await session.list_resources()

            if list_only:
                print(_json_dump({"tools": [t.name for t in tools.tools], "resources": [str(r.uri) for r in resources.resources]}))
                return

            if stats:
                res = await session.read_resource(AnyUrl("stats://graphrag"))
                first = res.contents[0]
                if isinstance(first, types.TextContent):
                    print(first.text)
                else:
                    print(_json_dump({"contents": res.contents}))
                return

            if search:
                result = await session.call_tool(
                    "graphrag_search",
                    arguments={"query": search},
                )
                structured = getattr(result, "structuredContent", None)
                if structured is not None:
                    print(_json_dump(structured))
                else:
                    print(_json_dump({"content": [getattr(c, "text", str(c)) for c in result.content], "isError": result.isError}))
                return

            if generate:
                result = await session.call_tool(
                    "graphrag_generate",
                    arguments={"query": generate},
                )
                structured = getattr(result, "structuredContent", None)
                if structured is not None:
                    print(_json_dump(structured))
                else:
                    print(_json_dump({"content": [getattr(c, "text", str(c)) for c in result.content], "isError": result.isError}))
                return

            print("No action specified. Use --list / --stats / --search / --generate.")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True, help="Path to config.json used to start the server")
    p.add_argument("--list", action="store_true", help="List tools and resources (no embedding/LLM calls)")
    p.add_argument("--stats", action="store_true", help="Read stats://graphrag resource (no embedding/LLM calls)")
    p.add_argument("--search", default=None, help="Call graphrag_search (calls embedding; may incur cost)")
    p.add_argument("--generate", default=None, help="Call graphrag_generate (calls chat LLM; may incur cost)")
    args = p.parse_args()

    asyncio.run(
        _run(
            config_path=args.config,
            list_only=bool(args.list),
            stats=bool(args.stats),
            search=args.search,
            generate=args.generate,
        )
    )


if __name__ == "__main__":
    main()

