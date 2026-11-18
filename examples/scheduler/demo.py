"""Run the scheduler end to end in a throwaway workspace. Nothing leaves this machine.

    python examples/scheduler/demo.py

It runs the example jobs once, shows the history, and serves the read-only
status page on an ephemeral loopback port long enough to fetch /api/status.
systemd is not contacted.
"""

from __future__ import annotations

import json
import tempfile
import threading
import urllib.request
from pathlib import Path

from sovereign_m.scheduler.config import load_settings
from sovereign_m.scheduler.runner import Runner
from sovereign_m.scheduler.statuspage import StatusApp, make_server


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        settings = load_settings(workspace=Path(tmp))
        runner = Runner(settings)
        for job in settings.jobs:
            if job.enabled:
                for attempt in runner.run_job(job):
                    print(f"{attempt.job}: attempt {attempt.attempt} {attempt.status}")
        app = StatusApp(settings, store=runner.store)  # no external timers in the demo
        server, _ = make_server(app, "loopback", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        host, port = server.server_address[:2]
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/api/status", timeout=10) as resp:
                data = json.load(resp)
        finally:
            server.shutdown()
            server.server_close()
        print(f"status page saw {len(data['jobs'])} jobs and {len(data['recent'])} recent runs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
