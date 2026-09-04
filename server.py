"""Local-only MCP server for network-interface configuration and stats.

Thin layer: validate tool arguments, call the platform backend in netinfo,
return JSON-ready dicts. All the risky binary parsing lives in netinfo, away
from FastMCP, so it is testable from a plain REPL with no MCP client.

Transport is stdio (MCPServer default): the MCP client spawns this process and
talks over stdin/stdout. Nothing binds a port, so "local-only" is structural.
"""

import dataclasses
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import netinfo

app = MCPServer("omp-net-mcp")


@app.tool(
    description="List local network interfaces with name, index, MTU, MAC, admin/running state, and operational state.",
    structured_output=True,
)
def list_interfaces() -> list[dict[str, Any]]:
    """Return a normalized list of local network interfaces."""
    return [dataclasses.asdict(i) for i in netinfo.list_interfaces()]


@app.tool(
    description="Return details for one local network interface by name: MTU, MAC, admin/running state, and operational state.",
    structured_output=True,
)
def get_interface(name: str) -> dict[str, Any]:
    """Return details for one local interface."""
    try:
        return dataclasses.asdict(netinfo.get_interface(name))
    except LookupError as exc:
        # Deliberate "unknown interface" error; keep the message readable
        # instead of mcp's generic crash wrapper.
        raise ToolError(str(exc)) from exc


@app.tool(
    description="Return RX/TX byte, packet, error, and drop counters (cumulative since interface up, not rates).",
    structured_output=True,
)
def get_interface_statistics(name: str) -> dict[str, Any]:
    """Return RX/TX counters for one local interface."""
    try:
        return dataclasses.asdict(netinfo.get_interface_statistics(name))
    except LookupError as exc:
        raise ToolError(str(exc)) from exc


@app.tool(
    description="Return default routes for the given address family, with interface, gateway, metric, and family.",
    structured_output=True,
)
def get_default_routes(
    family: Literal["ipv4", "ipv6", "all"] = "all",
) -> list[dict[str, Any]]:
    """Return default-route details for ipv4, ipv6, or all."""
    try:
        return [dataclasses.asdict(r) for r in netinfo.get_default_routes(family)]
    except (LookupError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


def main() -> None:
    app.run(transport="stdio")


if __name__ == "__main__":
    main()
