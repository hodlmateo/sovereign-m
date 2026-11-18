# scheduler

One job runner and a read-only status page, standard library only.

- Runs jobs from a small YAML file, each in a subprocess, with a timeout,
  retries with backoff, and a file lock so a job never overlaps itself, even
  across two runner processes.
- Records every attempt in a private SQLite file, with an excerpt of the end
  of the output (last 4 KB, secrets redacted).
- Can **read** your existing systemd user timers and show them next to the
  jobs. It never starts, stops, enables, disables, or edits anything in systemd.
- Serves a status page (HTML and JSON) on the loopback address, meant to sit
  behind the gateway as a route. It has no buttons and no POST endpoints.

```bash
python -m sovereign_m.scheduler run                  # loop: run jobs as they come due
python -m sovereign_m.scheduler run --once           # one pass, then exit
python -m sovereign_m.scheduler run-job NAME         # run one job now (--force for a disabled one)
python -m sovereign_m.scheduler list                 # jobs plus external timers, next/last/result
python -m sovereign_m.scheduler import-timers        # print the read-only systemd import
python -m sovereign_m.scheduler status-page          # http server on loopback
python -m sovereign_m scheduler list                 # same verbs through the root CLI
python examples/scheduler/demo.py                    # safe demo in a throwaway workspace
```

Every verb accepts `--config PATH` (default `data/scheduler/config.yaml`).
Exit codes: 0 fine, 1 a job failed, 2 bad usage or refused bind, 3 `run-job`
skipped because the lock was held.

## Files

| Path | What |
|------|------|
| `data/scheduler/config.yaml` | runner, status page and systemd import settings |
| `data/scheduler/jobs.yaml` | example job definitions (harmless) |
| `<workspace>/scheduler/scheduler.sqlite` | attempt history, mode 0600 |
| `<workspace>/scheduler/locks/` | lock files, mode 0600 in a 0700 directory |
| `<workspace>/scheduler/work/` | default working directory for jobs |

The workspace is `sovereign_m.paths.workspace_dir()`: `SOVEREIGN_M_WORKSPACE`
if set, else `var/` in the checkout (gitignored).

## Job reference

```yaml
jobs:
  - name: nightly-python          # letters, digits, _ . - (used in lock file names)
    command:                      # argv list, run with no shell
      - "{python}"
      - -c
      - "print('hello')"
    schedule: "30 2 * * *"        # cron subset or "every 5m"
    timeout_seconds: 30           # default 60; the whole process group is killed on timeout
    retries: 2                    # default 0, max 10
    retry_backoff_seconds: 10     # default 5; doubles each retry (10, 20, ...), capped at one hour
    working_dir: "{workspace}"    # optional; default <workspace>/scheduler/work
    env_allowlist:                # only these variables are passed on (plus PATH)
      - LANG
    lock_group: demo              # jobs in one group never overlap
    enabled: true
```

- **command**: an argv list. A plain string is split with `shlex` and still
  runs without a shell. Set `shell: true` with a string command if you really
  want `/bin/sh -c`. `{python}` becomes the running interpreter and
  `{workspace}` the workspace directory; `$VARS` are expanded when the file loads.
- **schedule**: `M H dom mon dow`. Each field takes `*`, `a`, `a-b`, `a,b`,
  `*/n`, `a-b/n`. Months and weekdays accept three-letter names; weekday 7 is
  Sunday. When both day-of-month and weekday are restricted, either one
  matching is enough (standard cron). `@hourly`, `@daily`, `@weekly`,
  `@monthly` also work. `every 30s`, `every 5m`, `every 2h`, `every 1d` run an
  interval after the previous start. Times are the machine's local time.
- **Catch-up**: a job with no history is due at once if it is an interval job,
  or if a cron slot fell in the last minute. After that it is due when its next
  slot after the last start has passed. A runner that was down misses nothing
  more than one run per job.
- **Env**: the job gets `PATH`, `SOVEREIGN_M_JOB`, and the allowlisted names
  that exist in the runner's environment. Nothing else leaks in.
- Unknown keys are an error, so a typo does not silently disable a limit.

### Statuses

`ok`, `failed` (non-zero exit, could not start), `timeout`, `skipped-locked`
(the job or its group was already running; recorded once and not retried),
`running`. A `running` row whose runner died is marked `failed` (orphaned) the
next time that job takes its lock.

### Locks

Each run takes an `flock` on `job-NAME.lock` and, if set,
`group-GROUP.lock` under the workspace. The locks are held across retries.
`flock` is released by the kernel when a process exits, so a crash cannot
wedge a job. Two runners (or `run-job` beside a running loop) cannot start the
same job at once.

### Redaction

Output excerpts and journal lines pass through a redaction step before they are
stored or shown: `key=...`, `token: ...`, `password`, `secret`, `Authorization`
values, `Bearer ...`, `sk-...` style keys, private key blocks, long hex strings
(32+) and long base64-looking strings. Redaction is a safety net, not a
license to print secrets; keep keys out of job output.

## Read-only systemd import

`import-timers` (and the status page, and `list`) calls only:

```text
systemctl --user list-timers --all --output=json     (falls back to plain text)
systemctl --user show UNIT -p Result -p ExecMainStatus -p ActiveState -p SubState
journalctl --user -u SERVICE -n N --no-pager -o json
```

Every command passes `systemd.guard` first. Its allowlist for `systemctl` is
`list-timers`, `show`, `status`, `list-units`, always as `systemctl --user VERB`;
anything else (`start`, `stop`, `restart`, `enable`, `disable`, `daemon-reload`,
`kill`, `mask`, `edit` ...) raises `SystemdRefused` before a process is created.
Tests prove each of those is refused. The module never writes a unit file and
never disables or replaces a timer. If `systemctl` is missing, the import
reports that and the rest of the page works.

Settings (`systemd:` in `config.yaml`): `enabled`, `units` (fnmatch patterns for
timer names), `max_units`, `log_lines`, `cache_seconds`, `command_timeout`.

## Status page

```bash
python -m sovereign_m.scheduler status-page --port <SERVICE_PORT>
```

- `GET /` self-contained HTML (no external assets), refreshes every 30 s, with
  *Running / Next up*, *Recent runs* (status, time, duration, exit code) and
  *External systemd timers* (next, last, result, a few redacted log lines).
- `GET /api/status` the same data as JSON. `GET /health` is `{"ok": true}`.
- Anything but GET/HEAD gets 405. Output excerpts never appear on the page or in
  the JSON; they stay in the database.
- All values are HTML-escaped. Links are relative, so a route prefix works.
- It binds to the loopback address. `--host` takes `loopback`, `localhost`, or a
  literal address. A non-loopback address (including the all-interfaces
  addresses) is refused unless you pass `--allow-nonloopback` (or set
  `status_page.allow_nonloopback`), and even then it prints a warning. Host
  names are never resolved.
- An `X-M-Client` header from the gateway is shown on the page for display only,
  after stripping it to a short safe character set. It is never used to decide
  anything.

### Behind the gateway

The page is a plain upstream. A route in the gateway config could look like this
(adapt the field names to the gateway you run):

```yaml
routes:
  - name: scheduler
    prefix: /scheduler
    upstream: http://localhost:<SERVICE_PORT>
    methods: [GET, HEAD]          # the page is read-only; do not forward anything else
```

Let the gateway handle authentication and set `X-M-Client`. Keep the page bound
to loopback so the gateway is the only way in.

## Running the runner as one user service

Example only; this repository does not install it. Save it as
`$XDG_CONFIG_HOME/systemd/user/sovereign-scheduler.service`, then reload and
start it yourself.

```ini
[Unit]
Description=sovereign-m scheduler runner

[Service]
Type=simple
WorkingDirectory=%h/sovereign-m
Environment=SOVEREIGN_M_WORKSPACE=%S/sovereign-m
ExecStart=/usr/bin/python3 -m sovereign_m.scheduler run
Restart=on-failure
RestartSec=5
# Let a running job finish on stop, but not forever.
TimeoutStopSec=120

[Install]
WantedBy=default.target
```

`%h` is the user's home directory and `%S` the state directory (the user's
`$XDG_STATE_HOME`). Run a second unit for the status page the same way, with
`ExecStart=... status-page --port <SERVICE_PORT>`. One runner is enough; if two
ever run at once the locks keep jobs from overlapping.

## Migrating existing timers later (not done here)

The import only reads, so you can watch your current timers on the page first.
To move one timer at a time:

1. Pick one timer. Note its `ExecStart` command, schedule (`OnCalendar` or
   `OnUnitActiveSec`), and user/environment. Write it as a job with the closest
   schedule and an `env_allowlist` that covers what it needs.
2. Run `run-job NAME` by hand and compare the result and output with what the
   service does today.
3. Add the job as `enabled: false`, then switch it on while the old timer is
   still active. Both would run, so first give the job a `lock_group` the
   command itself respects, or accept one overlap window for an idempotent job.
4. When it has run correctly a few times, disable the old timer yourself:
   `systemctl --user disable --now NAME.timer`. The scheduler never does this.
5. **Rollback**: set the job to `enabled: false` and run
   `systemctl --user enable --now NAME.timer`. The old unit files were never
   touched, so nothing needs restoring. Keep them until the job has been
   stable for a few weeks.

Do one timer, wait, look at the page, then the next.
