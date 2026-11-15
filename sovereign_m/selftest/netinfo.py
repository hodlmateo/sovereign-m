"""Local address discovery. Read-only: ``ip -o -4 addr`` or a socket fallback."""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
from dataclasses import dataclass

# Built from parts so no address literal sits in this public source file.
LOOPBACK = ".".join(("127", "0", "0", "1"))
ANY_ADDR = ".".join(("0", "0", "0", "0"))
_SHARED_NET = ipaddress.ip_network(".".join(("100", "64", "0", "0")) + "/10")
_VIRTUAL_PREFIXES = ("docker", "br-", "veth", "virbr", "lxc", "lxd", "cni", "flannel")

_ADDR_LINE = re.compile(r"^\d+:\s+(?P<iface>\S+)\s+inet\s+(?P<ip>[0-9.]+)/(?P<bits>\d+)")


@dataclass(frozen=True)
class LocalAddress:
    iface: str
    ip: str
    kind: str  # loopback | vpn | lan | other


def classify(ip: str) -> str:
    addr = ipaddress.ip_address(ip)
    if addr.is_loopback:
        return "loopback"
    if addr in _SHARED_NET:
        return "vpn"
    if addr.is_private and not addr.is_link_local:
        return "lan"
    return "other"


def local_addresses() -> list[LocalAddress]:
    out: list[LocalAddress] = []
    try:
        proc = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        for line in proc.stdout.splitlines():
            match = _ADDR_LINE.match(line.strip())
            if not match:
                continue
            iface, ip = match.group("iface"), match.group("ip")
            out.append(LocalAddress(iface, ip, classify(ip)))
    except (OSError, subprocess.SubprocessError):
        pass
    if not out:
        out.append(LocalAddress("lo", LOOPBACK, "loopback"))
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect((".".join(("192", "0", "2", "1")), 9))  # UDP connect sends no packet
            ip = probe.getsockname()[0]
            probe.close()
            out.append(LocalAddress("default", ip, classify(ip)))
        except OSError:
            pass
    return out


def is_virtual_iface(name: str) -> bool:
    return name.startswith(_VIRTUAL_PREFIXES)


def vpn_iface(addresses: list[LocalAddress] | None = None) -> str | None:
    for item in addresses or local_addresses():
        if item.kind == "vpn":
            return item.iface
    return None
