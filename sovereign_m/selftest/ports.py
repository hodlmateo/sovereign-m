"""Check 1: which ports answer, and which services listen wider than they should.

Targets come from the Scope allowlist only. nmap runs as a plain TCP connect
scan (no version probes, no scripts) when it is installed; otherwise a small
pure-Python connect scan does the same job. ``ss -ltnp`` shows what each
service is bound to, and the firewall state is read without root.
"""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from sovereign_m.selftest import netinfo
from sovereign_m.selftest.report import Finding
from sovereign_m.selftest.scope import Scope

WILDCARDS = {netinfo.ANY_ADDR, "*", "::", "[::]"}


@dataclass(frozen=True)
class Listener:
    address: str   # as ss prints it, without brackets
    port: int
    process: str   # "" when ss cannot show it (another user's process)
    scope: str     # wide | loopback | vpn | lan | other | device


def parse_ss(text: str) -> list[Listener]:
    out: list[Listener] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] != "LISTEN":
            continue
        local = parts[3]
        host, _, port = local.rpartition(":")
        if not port.isdigit():
            continue
        host = host.strip("[]")
        process = ""
        match = re.search(r'users:\(\("([^"]+)"', line)
        if match:
            process = match.group(1)
        out.append(Listener(host, int(port), process, _listener_scope(host)))
    return out


def _listener_scope(host: str) -> str:
    device = ""
    if "%" in host:
        host, device = host.split("%", 1)
    if device == "lo":
        return "loopback"
    if host in WILDCARDS and not device:
        return "wide"
    if device:
        return "device"
    try:
        return netinfo.classify(host)
    except ValueError:
        return "other"


def read_listeners() -> list[Listener]:
    try:
        proc = subprocess.run(["ss", "-H", "-ltnp"], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    text = proc.stdout
    # ss -H drops the header on newer builds; our parser skips non-LISTEN lines anyway.
    return parse_ss(text)


def firewall_state() -> tuple[str, str]:
    """('active'|'inactive'|'unknown', evidence). Reads only; ``ufw status`` needs root."""
    evidence: list[str] = []
    service: str | None = None
    try:
        proc = subprocess.run(["systemctl", "is-active", "ufw"], capture_output=True, text=True, timeout=10, check=False)
        service = (proc.stdout.strip() or proc.stderr.strip()) or None
        evidence.append(f"systemctl is-active ufw: {service}")
    except (OSError, subprocess.SubprocessError):
        evidence.append("systemctl unavailable")
    enabled: str | None = None
    try:
        conf = Path("/etc/ufw/ufw.conf").read_text(encoding="utf-8")
        match = re.search(r"^ENABLED=(\w+)", conf, re.M)
        if match:
            enabled = match.group(1).lower()
            evidence.append(f"/etc/ufw/ufw.conf ENABLED={enabled}")
    except OSError:
        evidence.append("/etc/ufw/ufw.conf not readable")
    text = "; ".join(evidence)
    # ufw.service reports "active" even when ufw is disabled in its config (the
    # start script just exits), so ENABLED=no must win over the service state.
    if enabled == "no":
        return "inactive", text + " (the service can show active while ufw is disabled)"
    if service == "active" and enabled == "yes":
        return "active", text
    if service in {"inactive", "failed"}:
        return "inactive", text
    return "unknown", text


def parse_port_range(text: str) -> list[int]:
    ports: set[int] = set()
    for chunk in str(text).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo, hi = chunk.split("-", 1)
            lo_i, hi_i = int(lo), int(hi)
        else:
            lo_i = hi_i = int(chunk)
        if not (0 < lo_i <= hi_i < 65536):
            raise ValueError(f"bad port range: {chunk}")
        ports.update(range(lo_i, hi_i + 1))
    return sorted(ports)


def connect_scan(scope: Scope, hosts: list[str], ports: list[int], timeout: float = 0.5, workers: int = 100) -> dict[str, list[int]]:
    """Pure-Python TCP connect scan. Each host passes through the scope check first."""
    checked = [scope.require(host) for host in hosts]

    def probe(job: tuple[str, int]) -> tuple[str, int, bool]:
        host, port = job
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            return host, port, sock.connect_ex((host, port)) == 0
        except OSError:
            return host, port, False
        finally:
            sock.close()

    result: dict[str, list[int]] = {host: [] for host in checked}
    jobs = [(host, port) for host in checked for port in ports]
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        for host, port, ok in pool.map(probe, jobs):
            if ok:
                result[host].append(port)
    return {host: sorted(found) for host, found in result.items()}


def nmap_scan(scope: Scope, hosts: list[str], port_range: str, timing: int = 3) -> dict[str, list[int]]:
    checked = [scope.require(host) for host in hosts]
    cmd = ["nmap", "-sT", "-Pn", "-n", f"-T{int(timing)}", "-p", port_range, "--open", "-oX", "-", *checked]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, check=False)
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(f"nmap failed: {proc.stderr.strip()[:200]}")
    return parse_nmap_xml(proc.stdout)


def parse_nmap_xml(text: str) -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    root = ET.fromstring(text)
    for host in root.findall("host"):
        addr = host.find("address[@addrtype='ipv4']")
        if addr is None:
            continue
        ip = addr.get("addr", "")
        found = []
        for port in host.findall("ports/port"):
            state = port.find("state")
            if state is not None and state.get("state") == "open" and port.get("protocol") == "tcp":
                found.append(int(port.get("portid", "0")))
        result[ip] = sorted(found)
    return result


def scan(scope: Scope, cfg: dict, allow_nmap: bool = True) -> tuple[dict[str, list[int]], str]:
    hosts = scope.hosts()
    port_range = str(cfg["range"])
    if allow_nmap and cfg.get("use_nmap", True) and shutil.which("nmap"):
        try:
            return nmap_scan(scope, hosts, port_range, int(cfg.get("nmap_timing", 3))), "nmap (TCP connect scan, no version probes)"
        except (RuntimeError, OSError, subprocess.SubprocessError, ET.ParseError):
            pass
    found = connect_scan(scope, hosts, parse_port_range(port_range), float(cfg["connect_timeout"]), int(cfg["workers"]))
    return found, "built-in Python TCP connect scan"


def analyze(scope: Scope, cfg: dict, open_ports: dict[str, list[int]], listeners: list[Listener], firewall: tuple[str, str]) -> list[Finding]:
    findings: list[Finding] = []
    state, fw_evidence = firewall
    sensitive = {str(k): str(v) for k, v in (cfg.get("sensitive") or {}).items()}
    expected = {int(p) for p in (cfg.get("expected_wide") or [])}
    iface = netinfo.vpn_iface()
    ssh_port = next(iter(sorted(expected)), 22)
    safe_fw_fix = (
        "Turn on the host firewall without locking yourself out: first allow SSH"
        + (f" on the VPN interface ({iface})" if iface else "")
        + f" (port {ssh_port}), then set default deny for incoming, then enable it. "
        "Also rebind each service below to loopback or the VPN address so it does not rely on the firewall alone."
    )

    if state == "inactive":
        findings.append(Finding("ports", "ports.firewall.off", "med", "Host firewall (ufw) is not active",
                                f"Evidence: {fw_evidence}. With no firewall, every service bound to all interfaces is reachable from the whole LAN.",
                                safe_fw_fix, {"state": state}))
    elif state == "unknown":
        findings.append(Finding("ports", "ports.firewall.unknown", "low", "Could not tell whether the firewall is active",
                                f"Evidence: {fw_evidence}. `ufw status` needs root, so this check only reads the service state and config. UNVERIFIED.",
                                "Run `sudo ufw status verbose` yourself and compare.", {"state": state}))

    reachable = {(host, port) for host, ports in open_ports.items() for port in ports}
    lan_hosts = [h for h in open_ports if scope.allowed.get(h) == "lan"]

    seen: set[int] = set()
    for item in listeners:
        if item.scope != "wide" or item.port in seen:
            continue
        seen.add(item.port)
        via_lan = [h for h in lan_hosts if (h, item.port) in reachable]
        confirmed = (
            f"The scan connected to it on {', '.join(via_lan)}."
            if via_lan else "The scan did not connect on the LAN address (possibly filtered). UNVERIFIED."
        )
        label = item.process or "process not visible (owned by another user)"
        note = sensitive.get(str(item.port))
        if state == "inactive":
            if str(item.port) in sensitive:
                severity = "high"
            elif item.port in expected:
                severity = "low"
            else:
                severity = "med"
        else:
            severity = "low"
        detail = (
            f"Port {item.port} ({label}) listens on all interfaces. {confirmed}"
            + (f" Why it matters: {note}." if note else "")
            + (" The firewall is not active, so nothing else limits who can connect." if state == "inactive" else "")
        )
        if item.port in expected:
            fix = "Expected for remote login. Keep key-only login and limit it to the VPN interface in the firewall."
        else:
            fix = (
                f"Rebind port {item.port} to the loopback or VPN address in the service's own setting "
                "(restarting the service needs the owner's yes), and/or block it in the firewall."
            )
        findings.append(Finding("ports", f"ports.wide.{item.port}", severity,
                                f"Port {item.port} listens on all interfaces ({label})", detail, fix,
                                {"port": item.port, "process": item.process, "reached_on": via_lan}))

    # open ports the scan sees that ss did not list as wide: informational
    wide_ports = {i.port for i in listeners if i.scope == "wide"}
    for host in sorted(open_ports):
        extra = [p for p in open_ports[host] if p not in wide_ports and scope.allowed.get(host) == "lan"]
        if extra:
            findings.append(Finding("ports", f"ports.lan-extra.{host}", "med",
                                    f"Ports answer on the LAN address that `ss` did not show as all-interface: {extra}",
                                    "These are bound to the specific LAN address, so other machines on the LAN can reach them. UNVERIFIED which service owns them.",
                                    "Identify the owner with `ss -ltnp`, then rebind to loopback or the VPN address.",
                                    {"host": host, "ports": extra}))

    summary_lines = [f"{host} ({scope.allowed.get(host, '?')}): {', '.join(map(str, ports)) or 'none'}" for host, ports in sorted(open_ports.items())]
    findings.append(Finding("ports", "ports.inventory", "info", "Open TCP ports found per allowlisted address",
                            "\n".join(f"- {line}" for line in summary_lines), "", {"open": open_ports}))
    return findings
