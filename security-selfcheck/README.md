# Sovereign Security Self-Check

A free, zero-dependency, **defensive** host audit for a Linux box you own. Drop in one Python file, run it, and get a plain-English report — including a one-minute fix card for every warning.

No SaaS agent. No telemetry. No root required. No pip packages. The script only *reads* your machine, except for `--purple-test`, which creates and deletes a temporary decoy under `/tmp`.

This module lives in [hodlmateo/sovereign-m](https://github.com/hodlmateo/sovereign-m) so a solo operator or a small shop can copy the same checks into their own systems without paying for a SIEM, an EDR dashboard, or a consultant just to interpret an alert.

## What it checks

1. **Auth / SSH log rules (Sigma-inspired)** — looks for failed-login bursts and unexpected root SSH successes in readable auth logs or `journalctl`. If the log needs root, the check is skipped with a note — that is normal on many desktops.
2. **Persistence** — lists your user crontab and user systemd units/timers (`<HOME>/.config/systemd/user`) so an entry you did not install jumps out. Fill in `persistence.allowlist` later to flag anything new.
3. **Listening ports & exposure** — uses `ss` (or `/proc/net`) and flags listeners bound beyond loopback or a mesh-VPN range (`<MESH_VPN_V4>`, the usual <<TAILNET_NAME>_NAME> range). Expected ports go on `listen.exposed_ports_allowlist`.
4. **Firewall & updates** — checks whether `ufw` is active (gracefully handles non-root) and whether security updates are waiting (`apt` / update-notifier).
5. **Antivirus & YARA readiness** — checks that `clamscan` / `freshclam` (and optional `yara`) are installed, and that the ClamAV signature database is fresh.
6. **Honeyfile canary** — verifies a decoy file you never open (`--canary` or `canary.path` + `canary.expected_sha256`). A hash change raises **ALERT**.
7. **Backup freshness** — checks a backup folder or marker file (`--backup-dir` or `backup.path`) and warns when it is older than `backup.max_age_hours` (default 48).

Every `WARN`, `FAIL`, or `ALERT` prints a short remediation card a non-technical owner can follow.

## Quick start

```bash
# From this folder. Python 3 is enough — no venv, no pip.
python3 security_selfcheck.py

# Prove the canary check actually alerts (harmless, uses /tmp only).
python3 security_selfcheck.py --purple-test

# Machine-readable output for timers, dashboards, or a ticket.
python3 security_selfcheck.py --json
```

Useful flags:

```bash
python3 security_selfcheck.py --rules /path/to/rules.json
python3 security_selfcheck.py --canary /path/to/canary
python3 security_selfcheck.py --backup-dir /path/to/backups
python3 security_selfcheck.py --no-color
python3 security_selfcheck.py --help
```

Exit codes: `0` clean (OK / INFO / SKIP only), `2` warnings, `1` fail or alert. `--purple-test` returns `0` on `PURPLE TEST PASS`.

## Configure the rules

Copy `rules/default_rules.json` and pass `--rules`. The defaults are conservative:

- Failed-login burst: 8 failures in 10 minutes, 24-hour lookback.
- Root SSH success is flagged.
- Loopback (`<LOOPBACK_V4>`, `::1/128`) and mesh-VPN / CGNAT (`<MESH_VPN_V4>`) binds are treated as safe.
- `exposed_ports_allowlist` starts empty, so a service on `<WILDCARD_V4>` is a warning until you allow it.
- ClamAV signatures are stale after 7 days.
- Canary and backup paths start empty so a first run stays portable; set them when you are ready.

Do not put real hostnames, personal paths, or private addresses in a rules file you plan to publish. Use `$HOME` or `/path/to/canary`.

## Set up a honeyfile

```bash
mkdir -p "$HOME/.canary"
echo 'decoy' > "$HOME/.canary/passwords-backup.txt"
python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" \
  "$HOME/.canary/passwords-backup.txt"
```

Put that path and hash in `canary.path` and `canary.expected_sha256`, or pass `--canary`. Then run `--purple-test` once so you know an edit would be noticed.

## Daily timer (user systemd, no root)

`<HOME>/.config/systemd/user/security-selfcheck.service`:

```ini
[Unit]
Description=Sovereign Security Self-Check (read-only host audit)

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 %h/path/to/security-selfcheck/security_selfcheck.py --no-color
# Optional: append JSON to a local log
# ExecStart=/usr/bin/python3 %h/path/to/security-selfcheck/security_selfcheck.py --json
```

`<HOME>/.config/systemd/user/security-selfcheck.timer`:

```ini
[Unit]
Description=Run Sovereign Security Self-Check daily

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now security-selfcheck.timer
```

## What this is not

This is a **self-audit of a machine you own**. It does not scan other hosts, run exploits, bundle attack frameworks, or change firewall, package, or service state. If a check needs root (some auth logs, `ufw status`), it reports `SKIP` or `INFO` and tells you the one-minute alternative.

## License

MIT. Same license as the rest of this repository. See `LICENSE` at the repo root.
