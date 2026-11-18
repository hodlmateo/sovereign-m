import json
import os
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

from sovereign_m.cli import main as root_main
from sovereign_m.scheduler import locks, systemd
from sovereign_m.scheduler.config import DEFAULTS, ConfigError, Settings, _copy, load_settings, parse_jobs
from sovereign_m.scheduler.redact import redact, tail_redacted
from sovereign_m.scheduler.runner import Runner
from sovereign_m.scheduler.schedule import ScheduleError, is_due, next_run, parse_schedule
from sovereign_m.scheduler.statuspage import (
    BindRefused, StatusApp, build_status, clean_client, make_server, render_html, resolve_bind,
)
from sovereign_m.scheduler.store import Store
from sovereign_m.scheduler.systemd import CommandResult, ExternalTimer, SystemdReader, SystemdRefused

ROOT = Path(__file__).resolve().parents[1]
LOOP = ".".join(("127", "0", "0", "1"))
ANY = ".".join(("0", "0", "0", "0"))
LAN = ".".join(("192", "168", "7", "9"))


def dt(*parts):
    return datetime(*parts)


def make_settings(tmp_path, jobs, **cfg):
    config = _copy(DEFAULTS)
    config.update({"kill_grace_seconds": 0.5})
    config.update(cfg)
    ws = tmp_path / "ws"
    return Settings(cfg=config, jobs=parse_jobs(jobs, ws), workspace=ws)


def py_job(name, code, **extra):
    job = {"name": name, "command": ["{python}", "-c", code], "schedule": "every 1m"}
    job.update(extra)
    return job


# ---- schedules ---------------------------------------------------------------

def test_cron_next_run_steps_ranges_and_names():
    every15 = parse_schedule("*/15 * * * *")
    assert every15.next_after(dt(2026, 11, 18, 10, 7, 30)) == dt(2026, 11, 18, 10, 15)
    assert every15.next_after(dt(2026, 11, 18, 10, 45)) == dt(2026, 11, 18, 11, 0)
    weekdays = parse_schedule("0 9 * * mon-fri")
    # 2026-11-20 is a Friday; the next slot after it is Monday.
    assert weekdays.next_after(dt(2026, 11, 20, 9, 0)) == dt(2026, 11, 23, 9, 0)
    assert parse_schedule("30 2 * * *").next_after(dt(2026, 11, 18, 3, 0)) == dt(2026, 11, 19, 2, 30)
    assert parse_schedule("@daily").next_after(dt(2026, 12, 31, 23, 59)) == dt(2027, 1, 1, 0, 0)
    assert parse_schedule("0 0 1 jan *").next_after(dt(2026, 11, 18)) == dt(2027, 1, 1)


def test_cron_dom_or_dow_and_sunday_seven():
    both = parse_schedule("0 0 13 * fri")  # day 13 OR Friday
    assert both.next_after(dt(2026, 11, 12, 1, 0)) == dt(2026, 11, 13, 0, 0)  # a Friday
    assert both.next_after(dt(2026, 11, 13, 1, 0)) == dt(2026, 11, 20, 0, 0)
    sunday = parse_schedule("0 0 * * 7")
    assert sunday.next_after(dt(2026, 11, 18)) == dt(2026, 11, 22)
    assert parse_schedule("0 0 31 2 *").next_after(dt(2026, 1, 1)) is None


@pytest.mark.parametrize("bad", ["", "* * * *", "60 * * * *", "* 24 * * *", "*/0 * * * *", "5-1 * * * *",
                                 "a * * * *", "every 0s", "every five minutes", "* * * * * *", "1,,2 * * * *"])
def test_bad_schedules_are_rejected(bad):
    with pytest.raises(ScheduleError):
        parse_schedule(bad)


def test_interval_and_due_logic_with_injected_clock():
    iv = parse_schedule("every 5m")
    assert iv.seconds == 300 and parse_schedule("every 2h").seconds == 7200
    now = dt(2026, 11, 18, 12, 0, 0)
    assert is_due(iv, None, now)  # no history: due now
    assert not is_due(iv, dt(2026, 11, 18, 11, 57), now)
    assert is_due(iv, dt(2026, 11, 18, 11, 55), now)
    assert next_run(iv, dt(2026, 11, 18, 11, 58), now) == dt(2026, 11, 18, 12, 3)
    cron = parse_schedule("0 12 * * *")
    assert is_due(cron, None, dt(2026, 11, 18, 12, 0, 20))  # slot fell in the last minute
    assert not is_due(cron, None, dt(2026, 11, 18, 12, 5))
    assert not is_due(cron, dt(2026, 11, 18, 12, 0, 1), dt(2026, 11, 18, 12, 30))  # already ran this slot
    assert is_due(cron, dt(2026, 11, 17, 12, 0, 1), dt(2026, 11, 18, 12, 0, 5))


def test_due_jobs_uses_injected_clock(tmp_path):
    settings = make_settings(tmp_path, [
        py_job("tick", "pass", schedule="every 10m"),
        py_job("off", "pass", schedule="every 10m", enabled=False),
    ])
    clock = [datetime(2026, 11, 18, 12, 0).timestamp()]
    runner = Runner(settings, clock=lambda: clock[0])
    assert [j.name for j in runner.due_jobs()] == ["tick"]
    runner.run_job(settings.jobs[0])
    assert runner.due_jobs() == []
    clock[0] += 601
    assert [j.name for j in runner.due_jobs()] == ["tick"]


# ---- config ------------------------------------------------------------------

def test_shipped_config_and_jobs_load(tmp_path):
    settings = load_settings(workspace=tmp_path)
    names = [j.name for j in settings.jobs]
    assert "hello-echo" in names and len(names) == len(set(names))
    nightly = settings.job("nightly-python")
    assert nightly.retries == 2 and nightly.lock_group == "demo" and nightly.env_allowlist == ["LANG"]
    assert nightly.command[0] == sys.executable
    assert settings.job("weekday-report").enabled is False


def test_job_validation(tmp_path):
    ws = tmp_path
    for bad in (
        {"name": "x", "command": ["echo"], "schedule": "nope"},
        {"name": "../x", "command": ["echo"], "schedule": "every 1m"},
        {"name": "x", "command": [], "schedule": "every 1m"},
        {"name": "x", "command": ["echo"], "schedule": "every 1m", "bogus": 1},
        {"name": "x", "command": ["echo"], "schedule": "every 1m", "timeout_seconds": 0},
        {"name": "x", "command": ["echo"], "schedule": "every 1m", "env_allowlist": ["A B"]},
    ):
        with pytest.raises(ConfigError):
            parse_jobs([bad], ws)
    with pytest.raises(ConfigError):
        parse_jobs([{"name": "x", "command": ["a"], "schedule": "every 1m"}] * 2, ws)


# ---- running -----------------------------------------------------------------

def test_retries_then_success_with_backoff(tmp_path):
    counter = tmp_path / "count.txt"
    code = (
        "import pathlib,sys;p=pathlib.Path(sys.argv[1]);n=int(p.read_text() or 0)+1 if p.exists() else 1;"
        "p.write_text(str(n));print('try',n);sys.exit(0 if n>=3 else 5)"
    )
    job = py_job("flaky", code, retries=3, retry_backoff_seconds=2)
    job["command"].append(str(counter))
    settings = make_settings(tmp_path, [job])
    naps = []
    runner = Runner(settings, sleep=naps.append)
    attempts = runner.run_job(settings.jobs[0])
    assert [a.status for a in attempts] == ["failed", "failed", "ok"]
    assert [a.exit_code for a in attempts] == [5, 5, 0]
    assert naps == [2, 4]
    rows = runner.store.recent(10, "flaky")
    assert sorted(r["attempt"] for r in rows) == [1, 2, 3]
    assert "try 3" in attempts[-1].excerpt


def test_failure_after_all_retries_and_missing_program(tmp_path):
    settings = make_settings(tmp_path, [
        py_job("bad", "import sys;sys.exit(2)", retries=1, retry_backoff_seconds=0),
        {"name": "nothing", "command": ["definitely-not-a-real-program-xyz"], "schedule": "every 1m"},
    ])
    runner = Runner(settings, sleep=lambda s: None)
    assert [a.status for a in runner.run_job(settings.jobs[0])] == ["failed", "failed"]
    missing = runner.run_job(settings.jobs[1])
    assert missing[0].status == "failed" and missing[0].exit_code == 127


def test_timeout_kills_the_whole_process_group(tmp_path):
    code = (
        "import subprocess,sys,time;"
        "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "print(c.pid,flush=True);time.sleep(60)"
    )
    settings = make_settings(tmp_path, [py_job("slow", code, timeout_seconds=1)])
    started = time.time()
    attempt = Runner(settings).run_job(settings.jobs[0])[0]
    assert attempt.status == "timeout" and attempt.exit_code is None
    assert time.time() - started < 10
    child = int(attempt.excerpt.split()[0])
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("grandchild survived the timeout")


def test_env_allowlist_hides_other_variables(tmp_path):
    code = "import os;print('A=' + os.environ.get('SM_TEST_A','none'), 'B=' + os.environ.get('SM_TEST_B','none'))"
    settings = make_settings(tmp_path, [py_job("env", code, env_allowlist=["SM_TEST_A"])])
    runner = Runner(settings, environ={"PATH": os.environ["PATH"], "SM_TEST_A": "yes", "SM_TEST_B": "leak"})
    assert "A=yes B=none" in runner.run_job(settings.jobs[0])[0].excerpt


# ---- locks -------------------------------------------------------------------

def test_lock_prevents_overlap_and_records_skipped_locked(tmp_path):
    settings = make_settings(tmp_path, [
        py_job("a", "import time;time.sleep(2)", lock_group="g"),
        py_job("b", "print('b ran')", lock_group="g"),
        py_job("c", "print('c ran')"),
    ])
    runner = Runner(settings)
    a, b, c = settings.jobs
    results = {}
    thread = threading.Thread(target=lambda: results.update(a=runner.run_job(a)))
    thread.start()
    for _ in range(100):
        if runner.store.running():
            break
        time.sleep(0.05)
    assert runner.run_job(a)[0].status == "skipped-locked"  # same job
    assert runner.run_job(b)[0].status == "skipped-locked"  # same group
    assert runner.run_job(c)[0].status == "ok"  # unrelated job is not blocked
    thread.join()
    assert results["a"][0].status == "ok"
    statuses = [r["status"] for r in runner.store.recent(20)]
    assert statuses.count("skipped-locked") == 2
    assert runner.run_job(b)[0].status == "ok"  # lock released


def test_lock_holds_across_processes(tmp_path):
    config = tmp_path / "config.yaml"
    jobs = tmp_path / "jobs.yaml"
    script = tmp_path / "nap.py"
    script.write_text("import time\ntime.sleep(3)\n")
    jobs.write_text(
        "jobs:\n  - name: nap\n    command:\n      - \"{python}\"\n      - \"" + str(script) + "\"\n"
        "    schedule: \"every 1m\"\n    timeout_seconds: 20\n"
    )
    config.write_text("jobs_file: jobs.yaml\n")
    ws = tmp_path / "ws"
    env = dict(os.environ, SOVEREIGN_M_WORKSPACE=str(ws))
    proc = subprocess.Popen(
        [sys.executable, "-m", "sovereign_m.scheduler", "run-job", "nap", "--config", str(config)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        settings = load_settings(config, workspace=ws)
        store = Store(settings.db_path)
        for _ in range(100):
            if store.running():
                break
            time.sleep(0.1)
        assert store.running(), "the other process never started the job"
        attempt = Runner(settings, store=store).run_job(settings.jobs[0])[0]
        assert attempt.status == "skipped-locked"
    finally:
        proc.wait(timeout=30)


def test_lock_files_are_private(tmp_path):
    path = tmp_path / "locks" / "job-x.lock"
    with locks.hold([path]):
        with pytest.raises(locks.LockHeld):
            with locks.hold([path]):
                pass
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_stale_running_rows_are_marked_failed(tmp_path):
    settings = make_settings(tmp_path, [py_job("x", "print('hi')")])
    runner = Runner(settings)
    runner.store.start("x", "dead", 1, time.time() - 100, 999999)
    runner.run_job(settings.jobs[0])
    assert runner.store.running() == []
    assert "orphaned" in [r for r in runner.store.recent(5) if r["run_id"] == "dead"][0]["excerpt"]


# ---- store and redaction -----------------------------------------------------

def test_database_is_mode_0600(tmp_path):
    settings = make_settings(tmp_path, [py_job("x", "print('hi')")])
    runner = Runner(settings)
    runner.run_job(settings.jobs[0])
    assert stat.S_IMODE(settings.db_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(settings.db_path.parent.stat().st_mode) == 0o700
    existing = tmp_path / "old.sqlite"
    existing.write_bytes(b"")
    os.chmod(existing, 0o666)
    Store(existing)
    assert stat.S_IMODE(existing.stat().st_mode) == 0o600


def test_redaction_of_secret_looking_strings():
    fake_sk = "sk-" + "a1B2" * 6
    hexblob = "0123456789abcdef" * 3
    b64 = "Zm9vYmFy" * 6 + "9"
    text = "\n".join([
        "api_key=hunter2hunter2",
        'token: "abc def"',
        '{"password": "p@ss w0rd"}',
        "Authorization: Bearer abcdefghijklmnop",
        f"openai {fake_sk}",
        f"digest {hexblob}",
        f"blob {b64}",
        "-----BEGIN PRIVATE KEY-----\nMIIBsecretsecret\n-----END PRIVATE KEY-----",
        "plain line: everything fine",
    ])
    out = redact(text)
    for leaked in ("hunter2", "abc def", "w0rd", "abcdefghijklmnop", fake_sk, hexblob, b64, "MIIBsecret"):
        assert leaked not in out
    assert "plain line: everything fine" in out


def test_output_tail_is_limited_and_redacted_when_stored(tmp_path):
    secret = "tok" + "en=" + "x" * 20
    code = f"print('{secret}');print('y' * 20000);print('end marker')"
    settings = make_settings(tmp_path, [py_job("loud", code)])
    runner = Runner(settings)
    attempt = runner.run_job(settings.jobs[0])[0]
    assert len(attempt.excerpt.encode()) <= 4096
    assert attempt.excerpt.rstrip().endswith("end marker")
    assert "x" * 20 not in attempt.excerpt
    code2 = f"print('{secret}');print('end')"
    settings2 = make_settings(tmp_path, [py_job("quiet", code2)])
    again = Runner(settings2).run_job(settings2.jobs[0])[0]
    assert "[REDACTED]" in again.excerpt and "x" * 20 not in again.excerpt
    row = runner.store.recent(1)[0]
    assert "x" * 20 not in row["excerpt"]
    assert tail_redacted(b"\xff\xfe bad bytes") != ""


# ---- systemd import ----------------------------------------------------------

USEC = 1_700_000_000 * 1_000_000

TIMERS_JSON = json.dumps([
    {"next": USEC + 3_600_000_000, "left": 1, "last": USEC, "passed": 2, "unit": "backup.timer", "activates": "backup.service"},
    {"next": None, "left": None, "last": 0, "passed": 0, "unit": "idle.timer", "activates": "idle.service"},
])

TIMERS_TEXT = """\
NEXT                        LEFT          LAST                        PASSED       UNIT         ACTIVATES
Wed 2026-11-18 12:00:00 UTC 1h 2min left  Wed 2026-11-18 11:00:00 UTC 58min ago    backup.timer backup.service
n/a                         n/a           n/a                         n/a          idle.timer   idle.service

2 timers listed.
"""

SHOW = "Result=exit-code\nExecMainStatus=3\nActiveState=inactive\nSubState=dead\n"

JOURNAL = "\n".join([
    json.dumps({"MESSAGE": "started backup"}),
    json.dumps({"MESSAGE": "uploading with api_key=supersecretvalue"}),
    json.dumps({"MESSAGE": list(b"binary ok")}),
    "not json",
])


class Canned:
    def __init__(self, json_ok=True):
        self.calls = []
        self.json_ok = json_ok

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        if argv[0] == "journalctl":
            return CommandResult(0, JOURNAL)
        verb = argv[2]
        if verb == "list-timers":
            if "--output=json" in argv:
                return CommandResult(0, TIMERS_JSON) if self.json_ok else CommandResult(1, "", "unknown option")
            return CommandResult(0, TIMERS_TEXT)
        if verb == "show":
            return CommandResult(0, SHOW)
        raise AssertionError(argv)


def test_import_from_canned_json():
    canned = Canned()
    timers, error = systemd.import_timers(SystemdReader(canned), ["*"], 10, 5)
    assert error is None and [t.unit for t in timers] == ["backup.timer", "idle.timer"]
    backup = timers[0]
    assert backup.next_run == 1_700_000_000 + 3600 and backup.last_run == 1_700_000_000
    assert backup.result == "exit-code" and backup.exit_status == "3"
    assert backup.logs[0] == "started backup" and "binary ok" in backup.logs
    assert all("supersecretvalue" not in line for line in backup.logs)
    assert timers[1].next_run is None and timers[1].last_run is None
    # only read-only commands were issued
    for argv in canned.calls:
        assert argv[0] in {"systemctl", "journalctl"} and "--user" in argv
        if argv[0] == "systemctl":
            assert argv[2] in systemd.ALLOWED_VERBS


def test_import_falls_back_to_text_parsing():
    canned = Canned(json_ok=False)
    timers, error = systemd.import_timers(SystemdReader(canned), ["backup*"], 10, 0)
    assert error is None and [t.unit for t in timers] == ["backup.timer"]
    assert timers[0].next_run == 1_795_003_200  # 2026-11-18 12:00:00 UTC
    assert timers[0].activates == "backup.service"
    assert timers[0].logs == []  # log_lines=0 skips the journal
    parsed = systemd.parse_timers_text(TIMERS_TEXT)
    assert parsed[1].next_run is None and parsed[1].unit == "idle.timer"


def test_import_reports_missing_systemctl():
    def runner(argv, timeout):
        return CommandResult(127, "", "systemctl: not found")

    timers, error = systemd.import_timers(SystemdReader(runner))
    assert timers == [] and "failed" in error


@pytest.mark.parametrize("verb", ["start", "stop", "restart", "enable", "disable", "daemon-reload",
                                  "kill", "mask", "edit", "reload", "reset-failed", "set-property", "link"])
def test_state_changing_systemctl_verbs_are_refused(verb):
    calls = []
    reader = SystemdReader(lambda argv, timeout: calls.append(argv) or CommandResult(0))
    with pytest.raises(SystemdRefused):
        reader.systemctl(verb, "demo.service")
    with pytest.raises(SystemdRefused):
        reader.run(["systemctl", "--user", verb, "demo.service"])
    assert calls == []


def test_guard_refuses_odd_forms():
    for argv in (
        ["systemctl", "start", "x.service"],                      # no --user
        ["systemctl", "--user", "--now", "enable", "x.service"],  # verb must come first
        ["systemctl", "--user", "show", "--now"],
        ["systemctl", "--user", "show", "x;reboot"],
        ["systemctl", "--user", "show", "-x"],
        ["journalctl", "--user", "--vacuum-time=1s"],
        ["journalctl", "--user", "-u", "x.service", "-n", "5000", "--no-pager", "-o", "json"],
        ["journalctl", "--user", "-u", "-fx", "-n", "5", "--no-pager", "-o", "json"],
        ["sudo", "systemctl", "--user", "show"],
        [],
    ):
        with pytest.raises(SystemdRefused):
            systemd.guard(argv)
    for verb in ("list-timers", "show", "status", "list-units"):
        systemd.guard(["systemctl", "--user", verb])
    with pytest.raises(SystemdRefused):
        SystemdReader(Canned()).show("bad unit;name")


# ---- status page -------------------------------------------------------------

def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.headers, resp.read().decode("utf-8")


def evil_timers():
    t = ExternalTimer("<script>alert(1)</script>.timer", "x.service", 1_700_000_000.0, 1_699_990_000.0,
                      "exit-code", "3", "inactive", ["<img src=x onerror=alert(2)>"])
    return [t], None


def test_status_page_html_and_json_on_ephemeral_port(tmp_path):
    settings = make_settings(tmp_path, [
        py_job("ok-job", "print('fine')"),
        py_job("fail-job", "import sys;sys.exit(4)"),
        py_job("off-job", "pass", enabled=False),
    ])
    runner = Runner(settings)
    runner.run_job(settings.jobs[0])
    runner.run_job(settings.jobs[1])
    app = StatusApp(settings, store=runner.store, external=evil_timers)
    server, warning = make_server(app, "loopback", 0)
    assert warning is None and server.server_address[0] == LOOP
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://{LOOP}:{port}"
    try:
        status, headers, body = _get(base + "/", {"X-M-Client": '"><script>x</script>'})
        assert status == 200 and headers["Content-Type"].startswith("text/html")
        assert 'http-equiv="refresh" content="30"' in body
        for section in ("Running / Next up", "Recent runs", "External systemd timers"):
            assert section in body
        assert "ok-job" in body and "fail-job" in body and "disabled" in body
        assert "<script>" not in body and "&lt;script&gt;" in body
        assert "<img src=x" not in body and "&lt;img" in body
        assert "http://" not in body and "https://" not in body  # no external assets
        status, headers, raw = _get(base + "/api/status?x=1", {"X-M-Client": '"><script>x</script>'})
        data = json.loads(raw)
        assert data["read_only"] is True and data["client"] == "scriptxscript"
        assert {j["name"] for j in data["jobs"]} == {"ok-job", "fail-job", "off-job"}
        recent = {r["job"]: r for r in data["recent"]}
        assert recent["ok-job"]["status"] == "ok" and recent["ok-job"]["exit_code"] == 0
        assert recent["fail-job"]["status"] == "failed" and recent["fail-job"]["exit_code"] == 4
        assert data["external"][0]["result"] == "exit-code"
        assert "excerpt" not in raw  # output tails never leave the database
        assert _get(base + "/health")[2] == '{"ok": true}'
        with pytest.raises(urllib.error.HTTPError) as err:
            _get(base + "/nope")
        assert err.value.code == 404
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            req = urllib.request.Request(base + "/api/status", data=b"{}", method=method)
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(req, timeout=10)
            assert err.value.code == 405
    finally:
        server.shutdown()
        server.server_close()


def test_html_escapes_everything_even_unvalidated_values(tmp_path):
    settings = make_settings(tmp_path, [py_job("x", "pass")])
    status = build_status(settings, Store(settings.db_path), time.time(), evil_timers, "<b>me</b>")
    status["jobs"][0]["name"] = "<i>name</i>"
    status["jobs"][0]["lock_group"] = '"><svg>'
    status["external_error"] = "<u>oops</u>"
    page = render_html(status)
    for raw in ("<i>name", "<svg>", "<u>oops", "<b>me", "<script>", "<img"):
        assert raw not in page
    assert "&lt;i&gt;name" in page and "&lt;u&gt;oops" in page


def test_client_header_is_sanitised():
    assert clean_client("web-ui@gw.1") == "web-ui@gw.1"
    assert clean_client("<>\n ") is None and clean_client(None) is None
    assert len(clean_client("a" * 500)) == 64


def test_external_import_is_cached(tmp_path):
    settings = make_settings(tmp_path, [py_job("x", "pass")])
    calls = []
    now = [100.0]
    app = StatusApp(settings, external=lambda: calls.append(1) or ([], None), clock=lambda: now[0])
    app.status()
    app.status()
    assert len(calls) == 1
    now[0] += 1000
    app.status()
    assert len(calls) == 2


def test_non_loopback_bind_is_refused(tmp_path):
    for host in (ANY, "::", LAN, "example.test"):
        with pytest.raises(BindRefused):
            resolve_bind(host)
    assert resolve_bind("loopback") == (LOOP, None)
    assert resolve_bind("localhost") == (LOOP, None)
    assert resolve_bind("::1")[1] is None
    addr, warning = resolve_bind(ANY, allow_nonloopback=True)
    assert addr == ANY and "WARNING" in warning
    with pytest.raises(BindRefused):  # a name is never resolved, even with the flag
        resolve_bind("example.test", allow_nonloopback=True)
    settings = make_settings(tmp_path, [py_job("x", "pass")])
    with pytest.raises(BindRefused):
        make_server(StatusApp(settings), ANY, 0)


# ---- command line -----------------------------------------------------------------

def test_cli_status_page_refuses_non_loopback(capsys):
    assert root_main(["scheduler", "status-page", "--host", ANY, "--port", "0"]) == 2
    assert "refused" in capsys.readouterr().err


def test_cli_run_job_list_and_usage(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SOVEREIGN_M_WORKSPACE", str(tmp_path / "ws"))
    assert root_main(["scheduler", "run-job", "hello-echo"]) == 0
    assert "hello from the scheduler" in capsys.readouterr().out
    assert root_main(["scheduler", "run-job", "weekday-report"]) == 2  # disabled
    assert root_main(["scheduler", "run-job", "missing"]) == 2
    capsys.readouterr()
    assert root_main(["scheduler", "list", "--no-external"]) == 0
    out = capsys.readouterr().out
    assert "hello-echo" in out and "ok" in out and "disabled" in out
    assert root_main(["scheduler", "list", "--json", "--no-external"]) == 0
    assert json.loads(capsys.readouterr().out)["read_only"] is True
    assert root_main(["scheduler", "bogus"]) == 2
    assert root_main(["scheduler"]) == 0  # bare verb prints usage
    assert stat.S_IMODE((tmp_path / "ws" / "scheduler" / "scheduler.sqlite").stat().st_mode) == 0o600


def test_run_once_runs_due_jobs(tmp_path):
    settings = make_settings(tmp_path, [py_job("one", "print(1)"), py_job("two", "print(2)", schedule="0 0 1 1 *")])
    runner = Runner(settings)
    assert runner.run_once() == ["one"]
    assert runner.run_once() == []
