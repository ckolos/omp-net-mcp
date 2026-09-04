"""Linux network-interface data via pyroute2.

pyroute2 wraps the kernel's netlink channel (the same IPC `ip link` and
NetworkManager speak), so we get an atomic, kernel-native snapshot of every
interface without hand-rolling the binary protocol. Data still comes from the
kernel -- pyroute2 just does the struct packing/parsing we would otherwise
maintain ourselves.

Interface fields chosen per tool contract: name, index, MTU, MAC, admin/running
state, and RFC-2863 operational state. Flags are decoded to readable strings at
the boundary: these tools feed an LLM, and bitwise math is where a model goes
wrong -- we hand it names instead of a bitmask.
"""

from dataclasses import dataclass

from pyroute2 import IPRoute
from pyroute2.netlink.rtnl.ifinfmsg import IFF_NAMES

# pyroute2 decodes IFLA_OPERSTATE to these uppercase names already.
# Normalize to RFC 2863 lowercase for a stable, self-describing value.
_OPERSTATE_RENAME = {
    "UNKNOWN": "unknown",
    "NOTPRESENT": "notpresent",
    "DOWN": "down",
    "LOWERLAYERDOWN": "lowerlayerdown",
    "TESTING": "testing",
    "DORMANT": "dormant",
    "UP": "up",
    "LOWER_LAYER_DOWN": "lowerlayerdown",
}


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
    operstate: str | None  # RFC 2863 name


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


def _decode_flags(flag_bits: int) -> list[str]:
    """Decode a raw IFF_* bitmask into readable flag names.

    IFF_NAMES maps "IFF_UP" -> bit; strip the prefix for readability (the
    consumer is an LLM, and "UP" is more natural than "IFF_UP").
    """
    return [name[4:] for name, bit in IFF_NAMES.items() if flag_bits & bit]


def _decode_link(link) -> Interface:
    """Turn one pyroute2 link message into a normalized Interface."""
    name = link.get_attr("IFLA_IFNAME")
    if name is None:
        raise ValueError("link message missing IFLA_IFNAME")
    flags_bits = link.get("flags", 0)
    oper_upper = (link.get_attr("IFLA_OPERSTATE") or "UNKNOWN").upper()
    return Interface(
        name=name,
        index=link.get("index", 0),
        mtu=link.get_attr("IFLA_MTU") or 0,
        mac=link.get_attr("IFLA_ADDRESS") or None,
        flags=_decode_flags(flags_bits),
        admin_up=bool(flags_bits & 0x1),  # IFF_UP
        running=bool(flags_bits & 0x40),  # IFF_RUNNING
        operstate=_OPERSTATE_RENAME.get(oper_upper, oper_upper.lower()),
    )


def list_interfaces() -> list[Interface]:
    """Return a normalized list of all kernel-visible interfaces.

    A single malformed link message never hides the interfaces that parsed
    fine -- it is skipped, so the caller gets a slightly short list, never a
    crash.
    """
    with IPRoute() as ip:
        links = ip.get_links()

    interfaces = []
    for link in links:
        try:
            interfaces.append(_decode_link(link))
        except ValueError:
            continue
    return interfaces


def get_interface(name: str) -> Interface:
    """Return one interface by name, raising LookupError if unknown."""
    return _decode_link(_find_link(name))


def _find_link(name: str):
    """Return the pyroute2 link message for `name`, or raise LookupError."""
    with IPRoute() as ip:
        for link in ip.get_links():
            if link.get_attr("IFLA_IFNAME") == name:
                return link
    raise LookupError(f"no such interface: {name}")


def get_interface_statistics(name: str) -> InterfaceStats:
    """Return RX/TX counters for one interface."""
    link = _find_link(name)
    stats = link.get_attr("IFLA_STATS64") or {}
    # Only the counters we expose; missing fields are treated as 0.
    return InterfaceStats(
        name=name,
        rx_bytes=stats.get("rx_bytes", 0),
        rx_packets=stats.get("rx_packets", 0),
        rx_errors=stats.get("rx_errors", 0),
        rx_dropped=stats.get("rx_dropped", 0),
        tx_bytes=stats.get("tx_bytes", 0),
        tx_packets=stats.get("tx_packets", 0),
        tx_errors=stats.get("tx_errors", 0),
        tx_dropped=stats.get("tx_dropped", 0),
    )


# Route family -> netlink address family (AF_INET / AF_INET6).
_ROUTE_FAMILIES = {
    "ipv4": 2,
    "ipv6": 10,
    "all": 0,  # AF_UNSPEC: kernel returns both families in one dump
}


def get_default_routes(family: str = "all") -> list[Route]:
    """Return default routes (prefix length 0) for the requested family.

    `family` is "ipv4"|"ipv6"|"all". Each route resolved to interface name,
    gateway, metric (RTA_PRIORITY), and address family.
    """
    if family not in _ROUTE_FAMILIES:
        raise ValueError(
            f"family must be one of {sorted(_ROUTE_FAMILIES)}; got {family!r}"
        )
    af = _ROUTE_FAMILIES[family]

    routes = []
    with IPRoute() as ip:
        # Resolve OIF index -> name once from the same kernel snapshot.
        names = {l.get("index"): l.get_attr("IFLA_IFNAME") for l in ip.get_links()}
        for r in ip.get_routes(family=af):
            if r.get("dst_len") != 0:
                continue  # only default routes
            oif = r.get_attr("RTA_OIF")
            routes.append(
                Route(
                    interface=names.get(oif, "") if oif is not None else "",
                    gateway=r.get_attr("RTA_GATEWAY"),  # None for on-link
                    metric=r.get_attr("RTA_PRIORITY"),
                    family="ipv6" if r.get("family") == 10 else "ipv4",
                )
            )
    return routes
