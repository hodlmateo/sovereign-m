"""The scope allowlist. Every socket, nmap target and HTTP request goes through here.

Config lists targets as tokens or literal addresses:

  loopback   the machine's loopback address
  lan        this machine's private LAN addresses (not container bridges)
  vpn        this machine's shared-address-space (mesh VPN) addresses
  A.B.C.D    one literal IPv4 address you own

Ranges, CIDR blocks, hostnames (other than localhost) and anything that starts
with a dash are refused. Nothing outside the resolved set is ever contacted.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

from sovereign_m.selftest import netinfo

TOKENS = ("loopback", "lan", "vpn")


class ScopeError(Exception):
    """A target is not on the allowlist (or is not a plain single address)."""


def _parse_literal(text: str) -> str:
    text = text.strip()
    if text == "localhost":
        return netinfo.LOOPBACK
    if not text or text.startswith("-") or any(c in text for c in "/,; \t*"):
        raise ScopeError(f"not a single address: {text!r}")
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        raise ScopeError(f"hostnames and ranges are refused (use an address): {text!r}") from None
    if addr.version != 4:
        raise ScopeError(f"only IPv4 targets are supported: {text!r}")
    if addr.is_multicast or addr.is_unspecified or addr.is_reserved:
        raise ScopeError(f"not a host address: {text!r}")
    return str(addr)


@dataclass
class Scope:
    allowed: dict[str, str] = field(default_factory=dict)  # ip -> kind

    @classmethod
    def from_entries(cls, entries: list, addresses: list[netinfo.LocalAddress] | None = None) -> "Scope":
        addresses = addresses if addresses is not None else netinfo.local_addresses()
        allowed: dict[str, str] = {}
        for entry in entries or []:
            text = str(entry).strip()
            if text in TOKENS:
                for item in addresses:
                    if item.kind == text and not netinfo.is_virtual_iface(item.iface):
                        allowed[item.ip] = item.kind
                continue
            ip = _parse_literal(text)
            allowed[ip] = netinfo.classify(ip)
        return cls(allowed)

    def hosts(self) -> list[str]:
        return sorted(self.allowed)

    def kind(self, host: str) -> str:
        return self.allowed[self.require(host)]

    def require(self, host: str) -> str:
        """Return the normalized address or raise ScopeError. Never contacts anything."""
        ip = _parse_literal(str(host))
        if ip not in self.allowed:
            raise ScopeError(f"{ip} is not in the selftest allowlist (edit the config to add a machine you own)")
        return ip

    def resolve_service(self, text: str) -> tuple[str, int]:
        """Turn 'loopback:8460' or an 'ip:port' into (ip, port); each must be in scope."""
        host, _, port = str(text).rpartition(":")
        if not host or not port.isdigit() or not 0 < int(port) < 65536:
            raise ScopeError(f"service must look like host:port: {text!r}")
        if host in TOKENS:
            matches = [ip for ip, kind in self.allowed.items() if kind == host]
            if not matches:
                raise ScopeError(f"no {host} address is in scope on this machine")
            return sorted(matches)[0], int(port)
        return self.require(host), int(port)

    def expand_service(self, text: str) -> list[tuple[str, int]]:
        """Like resolve_service but a token yields one entry per matching address."""
        host, _, port = str(text).rpartition(":")
        if host in TOKENS and port.isdigit():
            return [(ip, int(port)) for ip, kind in sorted(self.allowed.items()) if kind == host]
        return [self.resolve_service(text)]
