"""gateway: one authenticated front door for local services.

A small reverse proxy on the standard library. It binds the loopback address
by default, gives each client its own key, scopes, and rate limit, applies a
path allowlist and denylist per route, writes an audit log with no bodies and
no keys, and runs an optional middleware chain (for example a cache).
"""

from sovereign_m import __version__

__all__ = ["__version__"]
