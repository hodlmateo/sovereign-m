"""Scan the checkout for the kinds of leaks this public demo must not carry."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", ".venv", "var", "__pycache__", ".pytest_cache", ".mypy_cache"}

EMAIL = re.compile(
    r"[A-Za-z0-9._%+\-]+"
    + "@"
    + r"[A-Za-z0-9.\-]+"
    + r"\.[A-Za-z]{2,}"
)
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
TAX_NUMBER = re.compile(r"\b\d{2}-\d{7}\b")
TAX_WORD = re.compile(r"\b" + ("E" + "IN") + r"\b")
# Built in pieces so this file does not contain the marker it searches for.
_MESH = "ta" + "il"
MESH_WORD = re.compile(r"\b" + _MESH + r"\b", re.IGNORECASE)
MESH_NET = re.compile(_MESH + "scale", re.IGNORECASE)
# User home paths (built in pieces so examples stay generic).
HOME_UNIX = re.compile(r"/ho" + r"me/" + r"[A-Za-z0-9._-]+")
HOME_MAC = re.compile(r"/Us" + r"ers/" + r"[A-Za-z0-9._-]+")
HOME_TILDE = re.compile(r"~/" + r"[A-Za-z0-9._/-]+")


def test_checkout_has_no_contact_address_or_mesh_markers():
    problems: list[str] = []
    for path in _files():
        text = _read(path)
        if text is None:
            continue
        relative = path.relative_to(ROOT).as_posix()
        for line_no, line in enumerate(text.splitlines(), start=1):
            # Site footers already publish a contact mailto; that is not a leak.
            email = EMAIL.search(line)
            if email and not (relative.startswith("docs/") and "mailto:" in line):
                problems.append(f"{relative}:{line_no} email")
            # <LOOPBACK_HOST> and <WILDCARD_V4> are not contact leaks; the operator guide
            # uses the latter to say services must not bind all interfaces.
            for token in IPV4.findall(line):
                if token not in {"<LOOPBACK_HOST>", "<WILDCARD_V4>"}:
                    problems.append(f"{relative}:{line_no} ip")
            if TAX_NUMBER.search(line) or TAX_WORD.search(line):
                problems.append(f"{relative}:{line_no} tax-id")
            if MESH_NET.search(line) or MESH_WORD.search(line):
                problems.append(f"{relative}:{line_no} mesh-word")
            if HOME_UNIX.search(line) or HOME_MAC.search(line) or HOME_TILDE.search(line):
                problems.append(f"{relative}:{line_no} home-path")
    assert problems == []


def _files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP for part in path.parts):
            continue
        yield path


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None
