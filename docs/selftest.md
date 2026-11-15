# selftest

A read-only self check for machines you own. It answers three questions:

1. **ports**: what is listening, and is anything open to the whole network that should not be?
2. **web**: which local web services answer with no login, and which look like they can change things?
3. **privesc**: if a program running as your account were hijacked, how easily could it become admin?

It looks. It never exploits, never guesses passwords, never sends a request that
changes anything, never restarts a service, and never touches a firewall.
Every suggested fix in the report is text for a human to apply.

```bash
python -m sovereign_m.selftest scan                      # all three checks
python -m sovereign_m.selftest scan --check ports,web    # pick checks
python -m sovereign_m.selftest scan --out-dir DIR        # where the report goes
python -m sovereign_m.selftest report                    # print the newest report
python -m sovereign_m selftest scan                      # same verbs through the root CLI
python examples/selftest/scan_local_demo.py              # safe demo on a throwaway server
```

Exit code is 1 when any HIGH finding exists, 2 for a refused target or bad usage, else 0.

## Scope: the allowlist is the safety rail

Targets come from `data/selftest/config.yaml`, key `scope.targets`, and nowhere
else. There is no `--target` flag. Entries are:

| Entry | Meaning |
|-------|---------|
| `loopback` | this machine's loopback address |
| `lan` | this machine's private LAN addresses (container bridges are left out) |
| `vpn` | this machine's mesh-VPN addresses (shared address space) |
| a single address | one machine you own |

Ranges, CIDR blocks, hostnames (other than `localhost`) and anything starting
with a dash are refused. Tokens are resolved from the machine's own interfaces
at run time, so the config holds no addresses. Every socket, every nmap target
list and every HTTP request passes through `Scope.require` first, and tests
check that a refused target opens no socket and starts no nmap.

To check a second machine you own, add its single address to your own config
copy (`--config FILE`). Only scan machines you are allowed to test.

## Check 1: ports

- Runs `nmap` as a TCP connect scan (`-sT -Pn -n -T3`, no version probes, no scripts) when it is installed. Otherwise a built-in Python connect scan does the same job.
- Reads `ss -ltnp` to see what each service is bound to. A service on all interfaces (the "any address" wildcard in `ss`, shown as `*` or `::`) is flagged; loopback and a specific VPN address are not.
- Reads the firewall state without root: `systemctl is-active ufw` and `ENABLED=` in `/etc/ufw/ufw.conf`. `ufw.service` can say "active" while ufw is disabled, so `ENABLED=no` wins. If neither settles it the report says **unknown** instead of guessing.
- Severity: wide listener + no active firewall is MED, or HIGH for ports listed under `ports.sensitive` in the config; expected ports (SSH) are LOW; with an active firewall everything wide is LOW.
- The scan runs from the machine itself. It shows what listens and answers there; it cannot prove what another machine on the network is let through. Test that from a second machine you own.

## Check 2: web

- GET only, no redirects followed, rate limited (`web.requests_per_second`), short timeouts. Only status, size, content type and a short redacted page title are kept; bodies are discarded.
- Services come from `web.services` plus (by default) every open port the ports check found.
- The path list is small and lives in the config. Any name that sounds like an action (reset, restart, stop, send, approve, run, wake, delete, upload, webhook, backup ...) is **dropped in code**, because a badly written service could run it on a plain GET. A test enforces this.
- A nonsense-path baseline detects catch-all apps and single-page apps so they do not flood the report.
- "May change state" is inferred only from: an OpenAPI document the service publishes itself, a 405 answer to GET, or a known banner (Ollama). These are marked UNVERIFIED and never called.
- Severity: no login on a LAN address is MED, or HIGH when state-changing endpoints or a sensitive port are involved; VPN-only is LOW or MED; loopback-only is INFO or LOW.

## Check 3: privesc (LinPEAS-lite)

Local only. For secret-looking files it reports path, type and permissions and
never opens them.

- `sudo -n -l`: passwordless rules (`NOPASSWD: ALL` is HIGH).
- Group membership: `docker`, `lxd`, `disk`, `shadow` are root-equivalent (HIGH).
- SUID/SGID files under the configured roots, compared with a list of normal ones; general-purpose tools with the bit (find, vim, python ...) are HIGH.
- World-writable files or folders in `PATH` and systemd unit folders (HIGH), plus writable folders outside the home folder.
- Secret-looking files under the home folder (`.env`, `*key*`, `*token*`, `*secret*`, credentials, SSH keys). World-readable ones are flagged; owner-only ones are listed as a LOW inventory. Tokenizer and model files, `.venv`, caches, and `.git` are skipped.
- Writable systemd user units.
- Real LinPEAS runs **only if it is already installed**. Nothing is downloaded or piped into a shell.

## Reports

`scan` prints the report and writes `report-YYYY-MM-DD.md` and `.json` into
`report.dir` (default `var/selftest`, gitignored) or `--out-dir`. If a report for
that day exists, a `-2`, `-3` suffix is used; nothing is overwritten. Findings are
sorted HIGH, MED, LOW, INFO, each with a plain-English suggested fix. A redaction
pass removes tokens, key blocks, `password=` style values and long secret-like
strings from everything written.

## Limits

- Heuristics, not a proof of safety. A clean report means these checks found nothing.
- The web path list is small on purpose. It is not a vulnerability scanner.
- IPv4 only. No UDP.
- The SUID "known" list is tuned for a stock Debian or Ubuntu desktop; expect to extend it.
- Not covered yet: SSH daemon settings, cron, kernel patch level, container configuration.
