"""selftest: a read-only self check for machines you own.

Three checks (ports, web, privesc) that only look. Nothing is exploited,
nothing is changed, and every network target must be on an allowlist that
comes from the config file.
"""

__all__ = ["scope", "ports", "web", "privesc", "report"]
