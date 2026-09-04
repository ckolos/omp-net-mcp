# omp-net-mcp

A local-only MCP server that reports local network interface configuration and
statistics. It exposes four tools through the Model Context Protocol so an LLM
client can inspect the host's network interfaces, counters, and default routes.

Both Linux and macOS are implemented and verified against host oracles (see
[AGENTS.md](AGENTS.md) for the per-platform backend notes).

## Tools

| Tool | Input | Return |
|---|---|---|
| `list_interfaces` | — | normalized list of local interfaces |
| `get_interface` | `name` | one interface: name, index, MTU, MAC, flags, admin/running state, operstate |
| `get_interface_statistics` | `name` | RX/TX bytes, packets, errors, dropped (cumulative since interface up, not rates) |
| `get_default_routes` | `family` = `ipv4` \| `ipv6` \| `all` | default routes: interface, gateway, metric, family |

## Design

- **Local-only by construction** — talks stdio (MCP's default transport). The
  client spawns the process and messages flow over stdin/stdout; nothing binds
  a port, so access control is plain OS process permissions.
- **Kernel-native data, minimal dependencies.** Linux reads interface and route
  state directly from the kernel via [`pyroute2`](https://github.com/kittennbf/pyroute2)
  (the same netlink channel `ip` uses). macOS reads the same kernel tables
  `ifconfig` / `netstat -ibn` print through
  [`psutil`](https://github.com/giampaolo/psutil) (its maintained BSD ABI
  bindings) plus a `route -n get` subprocess. Everything else is the Python
  standard library.
- **No inheritance.** The platform backend is picked by module selection in
  `netinfo/__init__.py`; each backend is plain functions + dataclasses.
- **LLM-ready normalization.** Flags are decoded to readable strings
  (`"UP"`, `"BROADCAST"`, …), `operstate` is an RFC-2863 name, and admin vs
  running state are separate booleans — so the model doesn't do bitwise math.

## Requirements

- Python 3.11+
- Linux or macOS (Linux reads netlink via `pyroute2`; macOS reads BSD kernel
  tables via `psutil` and shells to the system `route` binary)

## Install

Uses [uv](https://docs.astral.sh/uv/):

```sh
uv sync
```

`uv` creates a `.venv`, installs `mcp`, `pyroute2`, and `psutil` (psutil is only
imported on macOS), plus the `ruff` dev dependency, and exposes the
`omp-net-mcp` console command.

## Run / smoke

From the project root:

```sh
uv run omp-net-mcp
```

It blocks waiting for an MCP client on stdio. To smoke the data functions
directly (no MCP client needed):

```sh
uv run python -c "import netinfo, json; print(json.dumps([i.__dict__ for i in netinfo.list_interfaces()], indent=2))"
```


## Test with MCP Inspector

The official [MCP Inspector](https://github.com/modelcontextprotocol/inspector)
is a browser UI for exercising an MCP server over stdio. It runs as an npx
package — no install needed:

```sh
npx @modelcontextprotocol/inspector
```

This will start a local web UI (printed to the terminal) and open in the browser.
In the left panel, switch the transport to **stdio** and start the server with:

```sh
uv run omp-net-mcp
```

You can also do this and save some steps:

```sh
npx @modelcontextprotocol/inspector uv run python server.py
```

with the project root as the working directory. Then use the right panel to list
the tools and call them (`list_interfaces`, `get_interface`,
`get_interface_statistics`, `get_default_routes`) and inspect the structured
output.

> Requires Node.js (for `npx`). The inspector launches the stdio server itself
> from the command you give it, so it is independent of any MCP client config.

## Wiring into an MCP client

Point your MCP client's server command at `uv run omp-net-mcp` with the project
directory as its working directory.

Claude Desktop — add to `claude_desktop_config.json` (set `PROJECT_DIR` to the
checked-out path):

```json
{
  "mcpServers": {
    "netinfo": {
      "command": "uv",
      "args": ["run", "--directory", "PROJECT_DIR", "omp-net-mcp"]
    }
  }
}
```

Generic stdio client — a config entry like (set `PROJECT_DIR` to the checked-out
path):

```json
{
  "command": "PROJECT_DIR/.venv/bin/omp-net-mcp"
}
```

## Development

- Lint / format: `uv run ruff check .` and `uv run ruff format .` (ruff is a dev
  dependency).
- `uv run ruff format .` may reformat; re-read files after running it.

> **Type checking is incomplete — errors are expected.**
>
> Zed's Python LSP is [basedpyright](https://basedpyright.com), which is
> strict-by-default and banners `Any`. It is **not** a declared dependency of
> this project (see `pyproject.toml` — the dev group is `ruff` only) and has
> no project configuration, so when the editor type-checks the code it runs
> unchecked against the checker's defaults.
>
> The `netinfo` backends depend on `pyroute2` (Linux) and `psutil` (macOS),
> neither of which ships type stubs. Under basedpyright their member access
> resolves to `Unknown`/`Any`, which surfaces a steady stream of
> `reportUnknown*` warnings, and because the checker bans explicit `Any`, a
> handful of `reportExplicitAny` / `reportMissingTypeArgument` hits appear in
> `server.py`'s tool payloads. **These are expected and do not indicate a
> runtime problem** — the data path is exercised end-to-end against host
> oracles (see [AGENTS.md](AGENTS.md)).
>
> Making the codebase fully type-clean would mean hand-restating the two
> libraries' internal message shapes as `NamedTuple`s/`cast`s at every
> boundary — a large block of `Any`-free but hand-maintained type-shape code
> that must be kept in sync with dependencies we don't control, for zero
> runtime benefit. That effort was deliberately **not** taken; the warning
> noise is accepted as the cost of the minimal-dependency design.

## Layout

```
pyproject.toml        # deps, dev-deps, console entry omp-net-mcp
README.md
AGENTS.md             # project summary + macOS backend instructions
server.py             # MCPServer app + tool defs (thin glue)
netinfo/
  __init__.py         # OS dispatch: picks linux|darwin module by sys.platform
  linux.py            # Linux backend (pyroute2)
  darwin.py           # macOS backend (psutil + stdlib, `route` subprocess)
```

See [AGENTS.md](AGENTS.md) for the data contract and the per-platform backend
notes.
