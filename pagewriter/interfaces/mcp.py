"""Official MCP SDK stdio adapter forwarding to an already running local API."""
import asyncio
import base64
import json
from pathlib import Path

from .client import ApiClient
from .common import InterfaceError, failure
from .schema import TOOLS, OUTPUT_SCHEMA


def create_mcp_server(connection_file):
    try:
        from mcp.server.lowlevel import Server
        from mcp.types import (CallToolResult, ImageContent, ListToolsResult,
                               TextContent, Tool, ToolAnnotations)
        from jsonschema import Draft202012Validator, ValidationError
    except ImportError as exc:
        raise InterfaceError("missing_dependency", "MCP requires the optional agent extra: pip install 'maliang[agent]'.") from exc
    from pagewriter import __version__
    client = ApiClient(connection_file)

    async def list_tools(context, params):
        return ListToolsResult(tools=[Tool(name=name, description=spec["description"],
            inputSchema=spec["inputSchema"], outputSchema=OUTPUT_SCHEMA, annotations=ToolAnnotations(
                readOnlyHint=spec["readOnlyHint"], openWorldHint=False))
            for name, spec in TOOLS.items()])

    async def call_tool(context, params):
        name, arguments = params.name, params.arguments or {}
        spec = TOOLS.get(name)
        if spec is None:
            envelope = failure("unknown_method", "Unknown application method.")
        else:
            try:
                Draft202012Validator(spec["inputSchema"]).validate(arguments)
            except ValidationError:
                envelope = failure("invalid_params", "Tool arguments do not match the published schema.")
            else:
                try:
                    envelope = await asyncio.to_thread(client.call, name, arguments)
                except InterfaceError as exc:
                    envelope = {"ok": False, "error": exc.as_dict()}
        content = [TextContent(type="text", text=json.dumps(envelope, ensure_ascii=False, allow_nan=False))]
        if envelope.get("ok") and name == "preview.render":
            result = envelope.get("result", {})
            path = result.get("path") if isinstance(result, dict) else None
            if path:
                try:
                    path = Path(path)
                    if path.stat().st_size <= 8 * 1024 * 1024:
                        png = path.read_bytes()
                        if png.startswith(b"\x89PNG\r\n\x1a\n"):
                            content.append(ImageContent(type="image", mimeType="image/png",
                                                        data=base64.b64encode(png).decode("ascii")))
                except OSError:
                    pass  # Structured preview metadata remains usable.
        return CallToolResult(content=content, structuredContent=envelope,
                              isError=not envelope.get("ok", False))

    return Server("maliang", version=__version__, on_list_tools=list_tools,
                  on_call_tool=call_tool,
                  get_tool_input_schema=lambda name: TOOLS.get(name, {}).get("inputSchema"))


def run_mcp(connection_file):
    server = create_mcp_server(connection_file)

    async def run():
        from mcp.server.stdio import stdio_server
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())

    asyncio.run(run())
