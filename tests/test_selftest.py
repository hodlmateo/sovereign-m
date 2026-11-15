import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from sovereign_m.cli import main as root_main
from sovereign_m.selftest import netinfo, ports, privesc, report as rep, web
from sovereign_m.selftest.cli import main, run_scan
from sovereign_m.selftest.config import load_config
from sovereign_m.selftest.netinfo import LocalAddress
from sovereign_m.selftest.scope import Scope, ScopeError

LOOP = ".".join(("127", "0", "0", "1"))
OTHER = ".".join(("8", "8", "8", "8"))
PRIVATE = ".".join(("192", "168", "7", "9"))
SHARED = ".".join(("100", "64", "1", "2"))
ANY = ".".join(("0", "0", "0", "0"))

ADDRS = [
    LocalAddress("lo", LOOP, "loopback"),
    LocalAddress("eth0", PRIVATE, "lan"),
    LocalAddress("docker0", ".".join(("172", "17", "0", "1")), "lan"),
    LocalAddress("vpn0", SHARED, "vpn"),
]


def make_scope(entries=("loopback",)):
    return Scope.from_entries(list(entries), ADDRS)


# ---- scope -------------------------------------------------------------

def test_tokens_resolve_and_skip_container_bridges():
    scope = make_scope(["loopback", "lan", "vpn"])
    assert scope.hosts() == sorted([LOOP, PRIVATE, SHARED])
    assert scope.kind(PRIVATE) == "lan"


@pytest.mark.parametrize("bad", [OTHER, "example.com", PRIVATE + "/24", "-oN", "*", "", OTHER + "," + LOOP, "::1"])
def test_scope_refuses_non_allowlisted_or_odd_targets(bad):
    scope = make_scope(["loopback"])
    with pytest.raises(ScopeError):
        scope.require(bad)


def test_config_refuses_range_and_hostname_entries():
    for entry in (PRIVATE + "/24", "example.com", "-p"):
        with pytest.raises(ScopeError):
            Scope.from_entries([entry], ADDRS)


def test_literal_address_entry_is_allowed():
    scope = Scope.from_entries([OTHER], ADDRS)
    assert scope.require(OTHER) == OTHER


def test_connect_scan_refuses_before_opening_any_socket(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a socket was opened for a refused target")

    monkeypatch.setattr(socket, "socket", boom)
    with pytest.raises(ScopeError):
        ports.connect_scan(make_scope(), [LOOP, OTHER], [80])


def test_nmap_never_runs_for_refused_target(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("nmap was launched")

    monkeypatch.setattr(ports.subprocess, "run", boom)
    with pytest.raises(ScopeError):
        ports.nmap_scan(make_scope(), [OTHER], "1-10")


def test_fetcher_refuses_before_opening_any_socket(monkeypatch):
    monkeypatch.setattr(web.http.client, "HTTPConnection", lambda *a, **k: (_ for _ in ()).throw(AssertionError("connected")))
    fetcher = web.Fetcher(make_scope())
    with pytest.raises(ScopeError):
        fetcher.get(OTHER, 80, "")


def test_cli_exits_nonzero_for_non_allowlisted_target(tmp_path, capsys):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"scope:\n  targets:\n    - \"{OTHER}/24\"\n", encoding="utf-8")
    assert main(["scan", "--config", str(cfg), "--no-write", "--check", "ports"]) == 2
    assert "refused" in capsys.readouterr().out


def test_web_service_outside_scope_is_refused():
    cfg = load_config()
    cfg["scope"]["targets"] = ["loopback"]
    cfg["web"]["services"] = [f"{OTHER}:80"]
    with pytest.raises(ScopeError):
        run_scan(cfg, ["web"])


# ---- ports -------------------------------------------------------------

SS_SAMPLE = f"""\
LISTEN 0 4096 {LOOP}:8460 {ANY}:* users:(("python",pid=1,fd=4))
LISTEN 0 2048 {ANY}:8800 {ANY}:* users:(("uvicorn",pid=2,fd=4))
LISTEN 0 4096 *:11434 *:*
LISTEN 0 4096 {SHARED}%vpn0:8880 {ANY}:*
LISTEN 0 4096 {ANY}%lo:47176 {ANY}:* users:(("daemon",pid=3,fd=4))
LISTEN 0 5 {PRIVATE}:8480 {ANY}:* users:(("python3",pid=4,fd=3))
"""


def test_parse_ss_classifies_bind_scope():
    got = {item.port: item for item in ports.parse_ss(SS_SAMPLE)}
    assert got[8460].scope == "loopback"
    assert got[8800].scope == "wide" and got[8800].process == "uvicorn"
    assert got[11434].scope == "wide" and got[11434].process == ""
    assert got[8880].scope == "device"
    assert got[47176].scope == "loopback"
    assert got[8480].scope == "lan"


def test_analyze_flags_wide_listener_when_firewall_off():
    scope = make_scope(["loopback", "lan"])
    listeners = ports.parse_ss(SS_SAMPLE)
    cfg = load_config()["ports"]
    findings = ports.analyze(scope, cfg, {LOOP: [8800], PRIVATE: [8800, 8480, 11434]}, listeners, ("inactive", "test"))
    by_id = {f.id: f for f in findings}
    assert by_id["ports.firewall.off"].severity == "med"
    assert by_id["ports.wide.11434"].severity == "high"   # sensitive port, no firewall
    assert by_id["ports.wide.8800"].severity == "med"
    assert "ports.wide.8460" not in by_id                   # loopback only is fine
    assert by_id[f"ports.lan-extra.{PRIVATE}"].severity == "med"


def test_analyze_with_active_firewall_is_low():
    scope = make_scope(["loopback", "lan"])
    findings = ports.analyze(scope, load_config()["ports"], {PRIVATE: [8800]}, ports.parse_ss(SS_SAMPLE), ("active", "test"))
    wide = [f for f in findings if f.id.startswith("ports.wide")]
    assert wide and all(f.severity == "low" for f in wide)


def test_firewall_state_prefers_config_over_exited_service(monkeypatch, tmp_path):
    class P:
        stdout = "active\n"
        stderr = ""

    monkeypatch.setattr(ports.subprocess, "run", lambda *a, **k: P())
    conf = tmp_path / "ufw.conf"
    conf.write_text("ENABLED=no\n", encoding="utf-8")
    real_read = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: real_read(conf) if str(self) == "/etc/ufw/ufw.conf" else real_read(self, *a, **k))
    state, _ = ports.firewall_state()
    assert state == "inactive"


def test_firewall_unknown_is_not_guessed(monkeypatch):
    monkeypatch.setattr(ports.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no systemctl")))
    real_read = Path.read_text

    def fake(self, *a, **k):
        if str(self) == "/etc/ufw/ufw.conf":
            raise OSError("unreadable")
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", fake)
    assert ports.firewall_state()[0] == "unknown"


def test_port_range_parser():
    assert ports.parse_port_range("22,80-82") == [22, 80, 81, 82]
    with pytest.raises(ValueError):
        ports.parse_port_range("0-5")


def test_nmap_xml_parser():
    xml = f"""<nmaprun><host><address addr="{LOOP}" addrtype="ipv4"/><ports>
    <port protocol="tcp" portid="22"><state state="open"/></port>
    <port protocol="tcp" portid="23"><state state="closed"/></port></ports></host></nmaprun>"""
    assert ports.parse_nmap_xml(xml) == {LOOP: [22]}


# ---- local test server -------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    seen_methods: list[str] = []
    seen_paths: list[str] = []

    def log_message(self, *a):
        pass

    def _record(self):
        Handler.seen_methods.append(self.command)
        Handler.seen_paths.append(self.path)

    def do_GET(self):
        self._record()
        if self.path == "/":
            self._send(200, b"<html><title>Demo</title>ok</html>", "text/html")
        elif self.path == "/openapi.json":
            self._send(200, json.dumps({"paths": {"/api/go": {"post": {}}, "/api/read": {"get": {}}}}).encode(), "application/json")
        elif self.path == "/admin":
            self._send(401, b"no", "text/plain")
        elif self.path == "/api/chat":
            self._send(405, b"method", "text/plain")
        else:
            self._send(404, b"nope", "text/plain")

    def do_POST(self):
        self._record()
        self._send(200, b"changed", "text/plain")

    do_PUT = do_DELETE = do_PATCH = do_POST

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CatchAll(Handler):
    def do_GET(self):
        self._record()
        self._send(200, b"<html>app</html>", "text/html")


@pytest.fixture()
def server():
    def start(handler):
        Handler.seen_methods = []
        Handler.seen_paths = []
        srv = HTTPServer((LOOP, 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    started = []

    def factory(handler=Handler):
        srv = start(handler)
        started.append(srv)
        return srv.server_address[1]

    yield factory
    for srv in started:
        srv.shutdown()
        srv.server_close()


def test_python_connect_scan_finds_local_server(server):
    port = server()
    found = ports.connect_scan(make_scope(), [LOOP], [port, port + 1 if port < 65000 else port - 1], timeout=0.5)
    assert port in found[LOOP]


def test_web_probe_is_get_only_and_finds_endpoints(server):
    port = server()
    scope = make_scope()
    fetcher = web.Fetcher(scope, timeout=2, rps=500)
    words = web.clean_wordlist(["admin", "openapi.json", "api/chat", "api/other"])
    result = web.probe_service(fetcher, LOOP, port, "loopback", words)
    assert result.http
    assert Handler.seen_methods and set(Handler.seen_methods) == {"GET"}
    statuses = {w: r.status for w, r in result.hits}
    assert statuses == {"admin": 401, "openapi.json": 200, "api/chat": 405}
    assert result.openapi_writes == ["POST /api/go"]


def test_web_analysis_reports_unauth_state_changing_service_by_exposure(server):
    port = server()
    scope = make_scope()
    fetcher = web.Fetcher(scope, timeout=2, rps=500)
    result = web.probe_service(fetcher, LOOP, port, "loopback", ["openapi.json"])
    loop = web.analyze([result], {}, False)
    assert loop[0].severity == "low" and "POST /api/go" in loop[0].detail
    result.kind = "lan"
    twin = web.ServiceResult(PRIVATE, port, "lan", True, result.banner, result.catch_all, result.root, result.hits, result.openapi_writes)
    lan = web.analyze([result, twin], {}, False)
    assert len(lan) == 1 and lan[0].severity == "high"          # two LAN addresses, one finding
    assert PRIVATE in lan[0].detail and LOOP in lan[0].detail


def test_catch_all_service_does_not_flood_report(server):
    port = server(CatchAll)
    fetcher = web.Fetcher(make_scope(), timeout=2, rps=500)
    result = web.probe_service(fetcher, LOOP, port, "loopback", ["admin", "docs", "api"])
    assert result.catch_all and result.hits == []
    assert len(Handler.seen_paths) == 2  # root + the nonsense baseline only


def test_redirect_is_not_followed(server):
    class Redirect(Handler):
        def do_GET(self):
            self._record()
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()

    port = server(Redirect)
    resp = web.Fetcher(make_scope(), timeout=2, rps=500).get(LOOP, port, "")
    assert resp.status == 302 and resp.location == "/login"
    assert Handler.seen_paths == ["/"]


def test_wordlist_has_no_action_words_in_config_or_fallback():
    for words in (load_config()["web"]["wordlist"], web.SAFE_FALLBACK_WORDLIST):
        assert web.clean_wordlist(words) == words  # nothing removed, no duplicates
        assert not [w for w in words if web.is_unsafe_path(w)]


def test_action_words_are_dropped():
    dirty = ["reset", "api/restart", "send-now", "approve", "run", "webhook", "wake", "Delete", "upload", "admin", "docs"]
    assert web.clean_wordlist(dirty) == ["admin", "docs"]


def test_fetcher_source_only_sends_get():
    src = Path(web.__file__).read_text(encoding="utf-8")
    assert 'conn.request("GET"' in src
    for verb in ('"POST"', '"PUT"', '"DELETE"', '"PATCH"'):
        assert verb not in src


# ---- privesc -----------------------------------------------------------

def test_secret_kind_names():
    assert privesc.secret_kind(".env") == ".env file"
    assert privesc.secret_kind("id_ed25519") == "credential file"
    assert privesc.secret_kind("deploy_key") == "key-like file"
    assert privesc.secret_kind("tokenizer.json") is None
    assert privesc.secret_kind("keyboard.py") is None
    assert privesc.secret_kind("notes.md") is None
    assert privesc.secret_kind("cacert.pem") is None
    assert privesc.secret_kind("api_token") == "token-like file"


def test_secret_scan_reports_path_and_type_never_contents(tmp_path):
    secret = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
    good = tmp_path / ".env"
    good.write_text(f"API_KEY={secret}\n", encoding="utf-8")
    good.chmod(0o600)
    loose = tmp_path / "deploy_key"
    loose.write_text(secret, encoding="utf-8")
    loose.chmod(0o660)
    open_key = tmp_path / "other_secret"
    open_key.write_text(secret, encoding="utf-8")
    open_key.chmod(0o664)
    ssh = tmp_path / "id_rsa"
    ssh.write_text(secret, encoding="utf-8")
    ssh.chmod(0o644)
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "token").write_text("x", encoding="utf-8")
    findings = privesc.check_secrets(str(tmp_path), set(privesc._DEFAULT_PRUNE), 4)
    built = rep.build_report(findings, {"date": "2026-01-01"})
    blob = json.dumps(built) + rep.render_markdown(built)
    assert secret not in blob and "A1b2C3d4E5f6" not in blob
    sev = {f.id: f.severity for f in findings}
    assert sev["privesc.secret.loose.high"] == "high"      # world-readable ssh key
    assert sev["privesc.secret.loose.med"] == "med"        # other-readable, not an ssh key
    assert sev["privesc.secret.loose.low"] == "low"        # group-readable only
    assert sev["privesc.secret.inventory"] == "low"
    assert ".venv" not in blob


def test_group_findings():
    found = {f.id: f.severity for f in privesc.check_groups(["docker", "lxd", "adm", "users"])}
    assert found["privesc.group.docker"] == "high"
    assert found["privesc.group.lxd"] == "high"
    assert found["privesc.group.adm"] == "low"
    assert "privesc.group.users" not in found


def test_suid_known_and_risky(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("passwd", "weird-helper", "find"):
        f = bin_dir / name
        f.write_text("x", encoding="utf-8")
        f.chmod(0o4755)
    (bin_dir / "plain").write_text("x", encoding="utf-8")
    findings = privesc.check_suid([str(tmp_path)], ["passwd"], ["find"])
    ids = {f.id: f.severity for f in findings}
    assert ids["privesc.suid.risky.find"] == "high"
    assert ids["privesc.suid.unknown"] == "med"
    unknown = [f for f in findings if f.id == "privesc.suid.unknown"][0]
    assert "weird-helper" in unknown.detail and "passwd" not in unknown.detail


def test_world_writable_path_and_unit_dirs(tmp_path):
    path_dir = tmp_path / "pbin"
    path_dir.mkdir()
    (path_dir / "tool").write_text("x", encoding="utf-8")
    (path_dir / "tool").chmod(0o666)
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    unit_dir.chmod(0o777)
    findings = privesc.check_writable([str(path_dir)], [str(unit_dir)], "/nonexistent-home")
    sev = {f.id: f.severity for f in findings}
    assert sev[f"privesc.worldwritable.file.{path_dir / 'tool'}"] == "high"
    assert sev[f"privesc.worldwritable.dir.{unit_dir}"] == "high"


def test_sudo_parsing(monkeypatch):
    monkeypatch.setattr(privesc, "run", lambda cmd, timeout=20: (1, "sudo: a password is required"))
    assert privesc.check_sudo()[0].severity == "info"
    monkeypatch.setattr(privesc, "run", lambda cmd, timeout=20: (0, "User x may run:\n    (ALL) NOPASSWD: ALL"))
    assert privesc.check_sudo()[0].severity == "high"
    monkeypatch.setattr(privesc, "run", lambda cmd, timeout=20: (0, "(ALL) NOPASSWD: /usr/bin/systemctl restart demo"))
    out = privesc.check_sudo()[0]
    assert out.severity == "med" and "NOPASSWD" in out.detail


def test_linpeas_is_never_downloaded(monkeypatch, tmp_path):
    monkeypatch.setattr(privesc, "find_linpeas", lambda home: None)
    finding, note = privesc.linpeas_note(str(tmp_path), True)
    assert finding is None and "skipped" in note
    src = Path(privesc.__file__).read_text(encoding="utf-8")
    assert "curl" not in src and "wget" not in src and "urlopen" not in src


# ---- report ------------------------------------------------------------

def test_redaction():
    text = "password=hunter2 token: abc123 Bearer abcdefghijklmnop -----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----"
    out = rep.redact(text)
    assert "hunter2" not in out and "abc123" not in out and "abcdefghijklmnop" not in out and "MIIB" not in out
    assert "NOPASSWD: ALL" in rep.redact("(ALL) NOPASSWD: ALL")
    assert "/a/long/" + "x" * 45 + "/file" in rep.redact("/a/long/" + "x" * 45 + "/file")


def test_report_files_are_not_overwritten(tmp_path):
    finding = rep.Finding("ports", "x", "med", "T", "d", "f")
    built = rep.build_report([finding], {"date": "2026-02-03", "checks": ["ports"]})
    first = rep.write_report(built, tmp_path)
    second = rep.write_report(built, tmp_path)
    assert first[0].name == "report-2026-02-03.md" and first[1].name == "report-2026-02-03.json"
    assert second[0].name == "report-2026-02-03-2.md"
    data = json.loads(first[1].read_text(encoding="utf-8"))
    assert data["summary"]["med"] == 1 and data["findings"][0]["severity"] == "med"


def test_bad_severity_is_rejected():
    with pytest.raises(ValueError):
        rep.Finding("ports", "x", "critical", "T")


# ---- cli ---------------------------------------------------------------

def test_help_and_root_cli():
    assert main(["--help"]) == 0
    assert root_main(["selftest", "--help"]) == 0
    assert main(["bogus"]) == 2


def test_cli_scan_end_to_end_on_loopback(server, tmp_path, capsys):
    port = server()
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "scope:\n  targets:\n    - loopback\n"
        f"ports:\n  range: \"{port}\"\n  use_nmap: false\n  connect_timeout: 0.5\n"
        "web:\n  requests_per_second: 500\n  timeout: 2\n  wordlist:\n    - openapi.json\n    - admin\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    code = main(["scan", "--config", str(cfg), "--check", "ports,web", "--out-dir", str(out_dir), "--no-nmap"])
    assert code in (0, 1)
    assert set(Handler.seen_methods) == {"GET"}
    reports = list(out_dir.glob("report-*.json"))
    assert len(reports) == 1
    data = json.loads(reports[0].read_text(encoding="utf-8"))
    assert any(f["id"].startswith("web.open.") for f in data["findings"])
    assert main(["report", "--out-dir", str(out_dir)]) == 0
    assert "Selftest report" in capsys.readouterr().out


def test_default_config_loads_and_declares_scope():
    cfg = load_config()
    assert cfg["scope"]["targets"] == ["loopback", "lan", "vpn"]
    assert "http" not in " ".join(cfg["scope"]["targets"])


def test_web_terminal_banner_is_high_on_lan():
    root = web.Response(200, 10, "text/html", "ttyd - Terminal", "", "ttyd/1.7")
    res = web.ServiceResult(PRIVATE, 7681, "lan", True, "ttyd", False, root)
    found = web.analyze([res], {}, True)
    assert found[0].severity == "high" and "web terminal" in found[0].detail
    res.kind = "loopback"
    assert web.analyze([res], {}, True)[0].severity == "low"
