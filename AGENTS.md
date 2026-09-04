# AGENTS.md

## Project

`omp-net-mcp` is a local-only MCP server that reports local network interface
configuration and statistics. It exposes four tools:

| Tool | Input | Output |
|---|---|---|
| `list_interfaces` | none | normalized list of local interfaces |
| `get_interface` | `name` | one interface: name, index, MTU, MAC, flags, admin/running state, operstate |
| `get_interface_statistics` | `name` | RX/TX bytes, packets, errors, dropped (cumulative, not rates) |
| `get_default_routes` | `family` = `ipv4`\|`ipv6`\|`all` | default routes: interface, gateway, metric, family |

Design goals: small, idiomatic, no inheritance (backend chosen by module
selection, not subclassing), workstation standards, and the data comes kernel-
native. Linux and macOS are both implemented and verified against host oracles.

## Layout

```
pyproject.toml        # deps, dev-deps (ruff), console entry omp-net-mcp
server.py             # MCPServer app + @tool defs (thin glue; no platform logic)
netinfo/
  __init__.py         # OS dispatch: imports linux|darwin module by sys.platform
  linux.py            # Linux backend (pyroute2)
  darwin.py           # macOS backend (psutil + `route` subprocess)
```

`server.py` is thin: validate args, call a `netinfo` function, `dataclasses.asdict`
the result. All platform-specific parsing lives in the `netinfo` backend modules,
which are testable from a plain REPL with no MCP client.

## Data contract

`netinfo/__init__.py` imports a fixed symbol set from the active backend module:

- `Interface` (dataclass): `name, index, mtu, mac: str|None, flags: list[str],
  admin_up: bool, running: bool, operstate: str|None`
- `InterfaceStats` (dataclass): `name, rx_bytes, rx_packets, rx_errors, rx_dropped,
  tx_bytes, tx_packets, tx_errors, tx_dropped`
- `Route` (dataclass): `interface, gateway: str|None, metric: int|None, family`
- `list_interfaces() -> list[Interface]`
- `get_interface(name: str) -> Interface` — `raise LookupError` if unknown
- `get_interface_statistics(name: str) -> InterfaceStats` — `raise LookupError` if unknown
- `get_default_routes(family: str = "all") -> list[Route]` — family must be one of
  `ipv4|ipv6|all` (Linux maps to AF_INET/AF_INET6/AF_UNSPEC); `raise ValueError`
  on an invalid family

Normalization choices (keep consistent on macOS):

- MAC is lowercase `:`-joined, or `None` when absent.
- `flags` is a list of readable strings (`"UP"`, `"BROADCAST"`, `"RUNNING"` …),
  no `IFF_` prefix. These tools feed an LLM — decode bitmasks at the boundary.
- `admin_up` = administratively configured up (IFF_UP); `running` = carrier/L2
  (IFF_RUNNING). These are distinct.
- `operstate` is an RFC-2863 name: `unknown|notpresent|down|lowerlayerdown|
  testing|dormant|up` (Linux raises it from `IFLA_OPERSTATE`).

## Conventions

- Python 3.11+. Use `dataclasses`; no pydantic models in our code (mcp pulls
  pydantic in as a dependency but our code stays dataclass-simple).
- Use `str | None` (not `Optional[...]`) in type annotations.
- Lint/format with `ruff` (dev dependency): `uv run ruff check .` and
  `uv run ruff format .` must pass before finishing. Ruff may reformat — re-read
  files after formatting.
- Add a stub in **both** backends and update `__all__` in `netinfo/__init__.py`
  whenever a new data type / function is added to the contract, so both
  backends keep their clean seam against it.

## Verify

The MCP layer is exercised via a real stdio client (see the smoke pattern below —
not a test framework). The ground-truth oracle is the `ip` command's JSON output:
`ip -j link`, `ip -j route`, `ip -s link`. Compare your backend's output against it.

```python
# smoke: any backend function directly, no MCP needed
import netinfo

print(netinfo.list_interfaces())
```

```python
# end-to-end: spawn the server over stdio and call a tool
import asyncio, json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(command=".venv/bin/omp-net-mcp")
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool("list_interfaces", {})
            print(res.structured_content["result"])  # list tools wrap under "result"


asyncio.run(main())
```

mcp 2.x notes (verified):

- `FastMCP` is named `MCPServer` (`from mcp.server.mcpserver import MCPServer`).
- Returning a dataclass-backed `dict` from a tool yields `structured_content`
  directly (no `result` key); returning a `list` wraps it under `structured_content["result"]`.
- A deliberate tool error should be raised as `ToolError` from
  `mcp.server.mcpserver.exceptions` (message preserved; `is_error=True`).
  A bare `LookupError`/`ValueError` becomes a generic `UnexpectedToolError`.

## macOS backend notes (implemented)

`netinfo/darwin.py` is complete and verified on a macOS 15 (Darwin 24) host.
Key decisions, since they reverse earlier guidance:

- **psutil is approved** for the macOS data path (flags/MTU/MAC/counters). The
  original stdlib+ctypes attempt shipped three ABI bugs found only by oracle
  cross-checks: `socket(AF_LINK, SOCK_DGRAM)` fails EAFNOSUPPORT on current
  kernels; `SIOCGIFLLADDR` writes the MAC at `sa_data` offset 2 of the ifreq
  union (not 4) and returns EPERM for some interfaces (en6); the
  `net.link.generic.ifdata.<idx>.1` sysctl MIB no longer exists (ENOENT).
  psutil maintains these bindings upstream; data is still kernel-native.
- **Enumeration source is `socket.if_nameindex()`** (psutil has no interface
  index), so every kernel-visible interface appears even when psutil lacks an
  AF_LINK entry (gif0/stf0).
- **Default routes shell to `route -n get [-inet|-inet6] default`** — neither
  psutil nor the stdlib exposes a routing-socket dump. Recent route(8) prints
  no `metric:` line for a plain default; metric is then None. A missing
  default route can exit 0 with "not in table" on stderr and empty stdout —
  treat an unparseable report as "no route", not an error.
- **Flag vocabulary**: psutil's lowercase BSD names are uppercased to match
  linux.py. xnu aliases the retired IFF_NOTRAILERS bit (0x20) as IFF_SMART;
  psutil prints `notrailers`, ifconfig prints `SMART` — same bit, we emit
  NOTRAILERS. Linux's LOWER_UP/DORMANT have no BSD equivalent and never
  appear on macOS. `operstate` is derived from UP/RUNNING only (see
  `_operstate`).
- **macOS oracles** (no `ip`): `ifconfig -a` for flags/MTU/MAC;
  `netstat -ibn` `<Link#>` rows for counters (parse those rows specifically —
  per-address rows shift columns); `route -n get [-inet6] default`. Note
  psutil byte counters can trail a live interface's netstat snapshot by
  in-flight traffic; packets/errors must match exactly, bytes approximately.
