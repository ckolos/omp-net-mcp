"""Network-interface data collection, backend per platform.

Polymorphism by module selection, not inheritance: at import time we pick a
sibling module that exposes the same plain functions (list_interfaces, ...),
and server.py talks to that one name. Adding a new platform means adding a
module, not subclassing anything.
"""

import sys

if sys.platform.startswith("linux"):
    from .linux import (
        Interface,
        InterfaceStats,
        Route,
        get_default_routes,
        get_interface,
        get_interface_statistics,
        list_interfaces,
    )
elif sys.platform == "darwin":
    from .darwin import (
        Interface,
        InterfaceStats,
        Route,
        get_default_routes,
        get_interface,
        get_interface_statistics,
        list_interfaces,
    )
else:
    raise OSError(f"netinfo: unsupported platform {sys.platform!r}")

__all__ = [
    "Interface",
    "InterfaceStats",
    "Route",
    "get_default_routes",
    "get_interface",
    "get_interface_statistics",
    "list_interfaces",
]
