"""scheduler: one job queue/runner plus a read-only status page.

Jobs come from a small YAML file. Each run happens in a subprocess with a
timeout, retries with backoff, and a file lock so the same job (or lock group)
never overlaps, even across two runner processes. Attempts go to a private
SQLite file. Existing systemd user timers can be imported read-only and shown
beside the jobs. Nothing here starts, stops, or edits a systemd unit.
"""

__all__ = ["config", "schedule", "locks", "store", "runner", "systemd", "statuspage", "redact"]
