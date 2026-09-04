"""macOS network-interface data via psutil + stdlib.

macOS has no netlink. Its kernel-native analogs are BSD ioctls and the
NET_RT_IFLIST2 routing-socket/sysctl tables; hand-rolling those ABIs proved
fragile (AF_LINK sockets are not creatable as SOCK_DGRAM on recent kernels,
struct sockaddr.sa_data offsets differ per call site, and the
net.link.generic.ifdata MIB was removed), so we let psutil maintain them.
psutil reads the same kernel tables `ifconfig`/`netstat -ibn` print; data is
still kernel-native, just parsed by a maintained library instead of us.

Interface indices come from socket.if_nameindex() (psutil has no index); it
is also the enumeration source, so every kernel-visible interface appears
even when psutil lacks an AF_LINK entry for it (gif0/stf0). Default routes
shell to the system `route` binary: neither psutil nor the stdlib exposes a
routing-socket dump.

Flags are decoded to readable strings at the boundary: these tools feed an
LLM, and bitwise math is where a model goes wrong -- we hand it names instead
of a bitmask. psutil reports BSD flag names lowercase; we uppercase them to
match linux.py's vocabulary ("UP", "BROADCAST", ...). xnu aliases the retired
IFF_NOTRAILERS bit (0x20) as IFF_SMART; psutil prints it as "notrailers" and
ifconfig prints SMART -- we emit NOTRAILERS, the name shared with BSD.
Linux's LOWER_UP/DORMANT have no BSD equivalent and never appear on macOS.
"""

import socket
import subprocess
from dataclasses import dataclass

import psutil


@dataclass
class Interface:
    """One network interface, normalized for LLM consumption."""

    name: str
    index: int
    mtu: int
    mac: str | None
    flags: list[str]
    admin_up: bool  # IFF_UP: administratively configured up
    running: bool  # IFF_RUNNING: carrier/L2 present
    operstate: str | None  # RFC 2863 name, derived from flags (see _operstate)


@dataclass
class Route:
    """One default route (destination prefix length 0)."""

    interface: str | None  # name via index lookup; None if unresolvable
    gateway: str | None
    metric: int | None
    family: str  # "ipv4" or "ipv6"


@dataclass
class InterfaceStats:
    """RX/TX counters for one interface.

    All values are 64-bit cumulative counters since the interface came up --
    they are not rates. `drop` counts are distinct from `error`.
    """

    name: str
    rx_bytes: int
    rx_packets: int
    rx_errors: int
    rx_dropped: int
    tx_bytes: int
    tx_packets: int
    tx_errors: int
    tx_dropped: int


def _operstate(flags: set[str]) -> str:
    """Derive an RFC 2863 operstate from BSD interface flags.

    macOS exposes no IFLA_OPERSTATE equivalent, so we map from what IS available:

      UP and RUNNING    -> "up"             (configured up, carrier/L2 up)
      UP, not RUNNING   -> "lowerlayerdown" (configured, no carrier)
      neither           -> "down"

    The other RFC 2863 states are unreachable on macOS: "notpresent" -- a
    detached interface fails its lookups and is skipped from enumeration
    rather than reported; "dormant"/"testing" -- BSD has no IFF_DORMANT or
    exposed link-test mode to derive them from.
    """
    up = "UP" in flags
    running = "RUNNING" in flags
    if up and running:
        return "up"
    if up:
        return "lowerlayerdown"
    return "down"


def _mac(addr_list) -> str | None:
    """Link-layer address from a psutil net_if_addrs entry list, or None."""
    for addr in addr_list:
        if addr.family == socket.AF_LINK and addr.address:
            # psutil already yields lowercase colon-joined hex on macOS;
            # normalize defensively so the contract holds across versions.
            return addr.address.lower()
    return None


def _snapshot() -> tuple[dict, dict]:
    """One consistent pass over psutil's per-interface tables."""
    return (
        psutil.net_if_addrs(),
        psutil.net_if_stats(),
    )


def _build_interface(name: str, index: int, addrs: dict, stats: dict) -> Interface:
    stat = stats[name]
    # psutil emits comma-joined lowercase flag names; uppercase to linux.py's
    # vocabulary. Empty string means "no flags" (e.g. stf0).
    flags = [f.upper() for f in stat.flags.split(",") if f]
    flag_set = set(flags)
    return Interface(
        name=name,
        index=index,
        mtu=stat.mtu,
        mac=_mac(addrs.get(name, [])),
        flags=flags,
        admin_up="UP" in flag_set,
        running="RUNNING" in flag_set,
        operstate=_operstate(flag_set),
    )


def list_interfaces() -> list[Interface]:
    # if_nameindex() is the enumeration source: kernel-authoritative names +
    # indices, including interfaces psutil has no AF_LINK address for.
    addrs, stats = _snapshot()
    interfaces = []
    for index, name in socket.if_nameindex():
        try:
            interfaces.append(_build_interface(name, index, addrs, stats))
        except KeyError:
            # A detached/transient interface (awdl0 mid-cycle) can vanish
            # between enumeration and lookup; skip it -- the caller gets a
            # slightly short list, never a crash. Mirrors linux.py's rule.
            continue
    return interfaces


def get_interface(name: str) -> Interface:
    """Return one interface by name, raising LookupError if unknown."""
    addrs, stats = _snapshot()
    for index, cand in socket.if_nameindex():
        if cand == name:
            try:
                return _build_interface(name, index, addrs, stats)
            except KeyError as exc:
                raise LookupError(f"no such interface: {name}") from exc
    raise LookupError(f"no such interface: {name}")


def get_interface_statistics(name: str) -> InterfaceStats:
    """Return RX/TX counters for one interface.

    Source: psutil.net_io_counters(), the same 64-bit cumulative counters
    `netstat -ibn` prints (kernel sysctl/routing-socket tables).
    """
    # Existence via net_if_stats(); counters from the io table (skipping
    # net_if_addrs(), the most expensive of psutil's three passes).
    stats = psutil.net_if_stats()
    counters = psutil.net_io_counters(pernic=True).get(name)
    if counters is None or name not in stats:
        raise LookupError(f"no such interface: {name}")
    return InterfaceStats(
        name=name,
        rx_bytes=counters.bytes_recv,
        rx_packets=counters.packets_recv,
        rx_errors=counters.errin,
        rx_dropped=counters.dropin,
        tx_bytes=counters.bytes_sent,
        tx_packets=counters.packets_sent,
        tx_errors=counters.errout,
        tx_dropped=counters.dropout,
    )


# --- default routes ---------------------------------------------------------

# Route family -> the `route -n get` address-family flag. (Darwin's AF_INET6
# is 30, not Linux's 10 -- but we shell to the system `route` binary, which
# takes -inet/-inet6, so no raw family numbers leak in here.)
_ROUTE_GET_ARGS = {
    "ipv4": ["-inet"],
    "ipv6": ["-inet6"],
}


def _parse_route_get(output: str, family: str) -> Route | None:
    """Parse one `route -n get` key-value report into a Route.

    Returns None when the report has no interface line (nothing usable).
    A link-only default (no `gateway:` line, e.g. some IPv6 cases) yields
    gateway=None, mirroring Linux on-link defaults. Recent route(8) prints
    no `metric:` line for a plain default; metric is then None.
    """
    fields = {}
    for line in output.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    interface = fields.get("interface")
    if not interface:
        return None
    metric = fields.get("metric")
    return Route(
        interface=interface,
        gateway=fields.get("gateway"),
        metric=int(metric) if metric is not None else None,
        family=family,
    )


def get_default_routes(family: str = "all") -> list[Route]:
    """Return default routes (prefix length 0) for the requested family.

    `family` is "ipv4"|"ipv6"|"all". Uses the system `route -n get` binary --
    stdlib subprocess against a system tool, macOS' idiomatic read path since
    neither psutil nor the stdlib exposes a routing-socket dump. A missing
    default route for a family yields no entry; that is data, not an error.
    """
    if family not in _ROUTE_GET_ARGS and family != "all":
        raise ValueError(
            f"family must be one of {sorted([*_ROUTE_GET_ARGS, 'all'])}; got {family!r}"
        )

    wanted = list(_ROUTE_GET_ARGS) if family == "all" else [family]
    routes = []
    for fam in wanted:
        proc = subprocess.run(
            ["route", "-n", "get", *_ROUTE_GET_ARGS[fam], "default"],
            capture_output=True,
            text=True,
            check=False,  # missing default route -> empty report; that is data
        )
        if proc.returncode != 0:
            continue
        parsed = _parse_route_get(proc.stdout, fam)
        if parsed is not None:
            routes.append(parsed)
    return routes
