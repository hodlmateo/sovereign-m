"""Check 3: a LinPEAS-lite. Local, read-only, never prints a secret value.

Looks at: sudo rights, risky group membership, SUID files, world-writable
files and folders in PATH and systemd unit folders, writable user units, and
secret-looking files under the home folder (path, type and permissions only;
file contents are not read). Real LinPEAS is only run if it is already
installed; nothing is downloaded.
"""

from __future__ import annotations

import fnmatch
import grp
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

from sovereign_m.selftest.report import Finding

ROOT_EQUIVALENT_GROUPS = {
    "docker": "Members can start a container that mounts the whole disk as root.",
    "lxd": "Members can start a privileged container that mounts the whole disk as root.",
    "lxc": "Members can start a privileged container as root.",
    "disk": "Members can read and write raw disks, which exposes every file.",
    "shadow": "Members can read password hashes.",
}
NOTABLE_GROUPS = {
    "sudo": "Can use sudo (password normally required).",
    "wheel": "Can use sudo (password normally required).",
    "adm": "Can read most system logs, which can contain secrets.",
    "wireshark": "Can capture network traffic.",
}

_SECRET_TOKENS = {
    "key", "keys", "secret", "secrets", "token", "tokens", "credential", "credentials", "creds",
    "passwd", "password", "passwords", "apikey", "privkey", "private",
}
_SECRET_EXACT = {
    ".env", ".netrc", ".npmrc", ".pypirc", ".git-credentials", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "credentials", "credentials.json", "client_secret.json", "service-account.json", ".htpasswd", ".pgpass",
}
_SKIP_EXT = {".py", ".pyc", ".pyi", ".md", ".rst", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".c", ".h", ".rs", ".go",
             ".pub", ".example", ".sample", ".template", ".safetensors", ".gguf", ".bin", ".onnx", ".pt", ".pth", ".so", ".png", ".jpg", ".webp"}
_DEFAULT_PRUNE = {".venv", "venv", "node_modules", ".cache", ".git", "__pycache__", "site-packages", ".ollama", ".npm", ".cargo",
                  ".rustup", ".local/share/Trash", "snap", ".mozilla", ".config/google-chrome", ".config/chromium", "models", "var/lib"}


def run(cmd: list[str], timeout: float = 20) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return proc.returncode, (proc.stdout + proc.stderr)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)


# ---- sudo -------------------------------------------------------------

def check_sudo() -> list[Finding]:
    code, out = run(["sudo", "-n", "-l"])
    text = out.strip()
    if code != 0 and re.search(r"password is required|a terminal is required", text, re.I):
        return [Finding("privesc", "privesc.sudo.password", "info", "sudo needs a password",
                        "`sudo -n -l` was refused without a password, so no passwordless sudo rule is visible to this account.")]
    if code != 0:
        return [Finding("privesc", "privesc.sudo.unknown", "info", "sudo rights could not be listed",
                        f"`sudo -n -l` exited {code}. UNVERIFIED.")]
    if re.search(r"NOPASSWD:\s*ALL\b", text):
        return [Finding("privesc", "privesc.sudo.nopasswd-all", "high", "This account has passwordless sudo for everything",
                        "Anything running as this user (a bug in any local service, a bad package) is root.",
                        "Remove the NOPASSWD: ALL rule with visudo, or limit it to the exact commands needed.")]
    if "NOPASSWD" in text:
        rules = [line.strip() for line in text.splitlines() if "NOPASSWD" in line]
        return [Finding("privesc", "privesc.sudo.nopasswd", "med", "Some sudo commands need no password",
                        "Rules: " + " | ".join(rules[:6]), "Check each rule is needed and cannot be turned into a shell.")]
    return [Finding("privesc", "privesc.sudo.listed", "info", "sudo rules need a password", "")]


# ---- groups -----------------------------------------------------------

def check_groups(group_names: list[str] | None = None) -> list[Finding]:
    if group_names is None:
        gids = os.getgroups() + [os.getgid()]
        names = []
        for gid in set(gids):
            try:
                names.append(grp.getgrgid(gid).gr_name)
            except KeyError:
                pass
    else:
        names = group_names
    out: list[Finding] = []
    for name in sorted(set(names)):
        if name in ROOT_EQUIVALENT_GROUPS:
            out.append(Finding("privesc", f"privesc.group.{name}", "high", f"This account is in the '{name}' group (root-equivalent)",
                               ROOT_EQUIVALENT_GROUPS[name] + " Any program running as this user can take over the machine.",
                               f"If you do not need it, remove the account from the group (`sudo gpasswd -d USER {name}`, then log out and in). "
                               "If you do need it, move the work to rootless containers and keep untrusted code away from this account."))
        elif name in NOTABLE_GROUPS:
            out.append(Finding("privesc", f"privesc.group.{name}", "low" if name != "sudo" and name != "wheel" else "info",
                               f"This account is in the '{name}' group", NOTABLE_GROUPS[name]))
    return out


# ---- SUID -------------------------------------------------------------

def find_suid(roots: list[str]) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if d not in {"proc", "sys", "dev"}]
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode) and st.st_mode & (stat.S_ISUID | stat.S_ISGID):
                    found.append((path, st.st_mode))
    return sorted(found)


def check_suid(roots: list[str], known: list[str], risky: list[str]) -> list[Finding]:
    out: list[Finding] = []
    known_set = set(known)
    risky_set = set(risky)
    unknown: list[str] = []
    for path, mode in find_suid(roots):
        name = os.path.basename(path)
        kind = "SUID" if mode & stat.S_ISUID else "SGID"
        if name in risky_set:
            out.append(Finding("privesc", f"privesc.suid.risky.{name}", "high", f"{kind} on {name} ({path})",
                               f"{name} is a general-purpose tool; with the {kind} bit it can usually be turned into a root shell.",
                               f"Remove the bit if you did not set it on purpose (`sudo chmod u-s,g-s {path}`)."))
        elif name not in known_set:
            unknown.append(f"{path} ({kind})")
    if unknown:
        out.append(Finding("privesc", "privesc.suid.unknown", "med", f"{len(unknown)} SUID/SGID file(s) not on the usual list",
                           "Not on the known list, so worth a look (they may be normal for this distro): " + "; ".join(unknown[:15]) + (" ..." if len(unknown) > 15 else ""),
                           "For each, check the owning package with `dpkg -S PATH`; remove the bit from anything you cannot explain.",
                           {"files": unknown}))
    return out


# ---- writable PATH / unit dirs ---------------------------------------

def _mode_str(st: os.stat_result) -> str:
    return oct(stat.S_IMODE(st.st_mode))


def check_writable(path_dirs: list[str], unit_dirs: list[str], home: str) -> list[Finding]:
    out: list[Finding] = []
    uid = os.getuid()
    for label, dirs, sev_world, sev_user in (("PATH", path_dirs, "high", "info"), ("systemd unit", unit_dirs, "high", "low")):
        for d in dirs:
            try:
                st = os.stat(d)
            except OSError:
                continue
            in_home = d == home or d.startswith(home.rstrip("/") + "/")
            if st.st_mode & stat.S_IWOTH and not (st.st_mode & stat.S_ISVTX):
                out.append(Finding("privesc", f"privesc.worldwritable.dir.{d}", sev_world, f"World-writable {label} folder: {d}",
                                   f"Mode {_mode_str(st)}. Any local user or service can drop a file here.",
                                   f"Remove other-write: `chmod o-w {d}` (owner's yes first)."))
            elif os.access(d, os.W_OK) and uid != 0 and not in_home:
                out.append(Finding("privesc", f"privesc.writable.dir.{d}", "high", f"{label} folder outside home is writable by this account: {d}",
                                   "A program running as this user can plant a command that root or another user will run.",
                                   "Make the folder owned by root and not group/other-writable."))
            elif os.access(d, os.W_OK) and in_home:
                out.append(Finding("privesc", f"privesc.writable.userdir.{d}", sev_user, f"{label} folder in your home is writable: {d}",
                                   "Normal for user-level setups, but anything running as you can persist here.",
                                   "Nothing to do unless you want stricter isolation."))
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for entry in entries[:2000]:
                try:
                    est = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(est.st_mode) and est.st_mode & stat.S_IWOTH:
                    out.append(Finding("privesc", f"privesc.worldwritable.file.{entry.path}", sev_world, f"World-writable file in a {label} folder: {entry.path}",
                                       f"Mode {_mode_str(est)}. Anyone can change what this file does.",
                                       f"`chmod o-w {entry.path}` (owner's yes first)."))
    return out


# ---- secret-looking files under home ---------------------------------

_SKIP_NAMES = {"trust tokens", "trust tokens-journal", "cacert.pem", "ca-certificates.crt"}


def secret_kind(name: str) -> str | None:
    low = name.lower()
    if low in _SKIP_NAMES:
        return None
    if low in _SECRET_EXACT:
        return "credential file" if low != ".env" else ".env file"
    if low.startswith(".env.") and not low.endswith((".example", ".sample", ".template")):
        return ".env file"
    ext = os.path.splitext(low)[1]
    if ext in _SKIP_EXT:
        return None
    tokens = {t for t in re.split(r"[^a-z0-9]+", low) if t}
    if ext in {".p12", ".pfx", ".kdbx", ".ppk"}:
        return "key or certificate file"
    if ext == ".pem":
        # Most .pem files are public certificates; only call it a key if the name says so.
        return "key or certificate file" if tokens & {"key", "private", "priv", "privkey", "secret"} else None
    if tokens & _SECRET_TOKENS:
        if "tokenizer" in low:
            return None
        if "key" in tokens or "keys" in tokens or "private" in tokens or "privkey" in tokens:
            return "key-like file"
        if "token" in tokens or "tokens" in tokens:
            return "token-like file"
        return "secret-like file"
    return None


def find_secret_files(home: str, prune: set[str], max_depth: int = 6) -> list[tuple[str, str, int]]:
    found: list[tuple[str, str, int]] = []
    base = home.rstrip("/")
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False, onerror=lambda e: None):
        rel = os.path.relpath(dirpath, base)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        keep = []
        for d in dirnames:
            relpath = d if rel == "." else os.path.join(rel, d)
            if d in prune or relpath in prune or depth >= max_depth:
                continue
            keep.append(d)
        dirnames[:] = keep
        for name in filenames:
            kind = secret_kind(name)
            if not kind:
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_size > 1_000_000:
                continue
            found.append((path, kind, stat.S_IMODE(st.st_mode)))
    return sorted(found)


def check_secrets(home: str, prune: set[str], max_depth: int) -> list[Finding]:
    files = find_secret_files(home, prune, max_depth)
    out: list[Finding] = []
    tiers: dict[str, list[tuple[str, str, int]]] = {"high": [], "med": [], "low": []}
    tight = [f for f in files if not f[2] & 0o077]
    for path, kind, mode in files:
        if not mode & 0o077:
            continue
        name = os.path.basename(path)
        real_key = name.startswith("id_") or kind == "key or certificate file"
        if mode & 0o004:
            tiers["high" if real_key else "med"].append((path, kind, mode))
        else:
            tiers["low"].append((path, kind, mode))
    wording = {
        "high": ("SSH or certificate key file(s) readable by every user on the machine", "Any local account or service can copy these."),
        "med": ("secret-looking file(s) readable by every user on the machine", "Any local account or service can read these (this box is mostly single-user, so the risk is a service account or a stolen low-privilege process)."),
        "low": ("secret-looking file(s) readable by their group", "Only members of the file's group can read them."),
    }
    for sev in ("high", "med", "low"):
        group = tiers[sev]
        if not group:
            continue
        title, why = wording[sev]
        listing = "\n".join(f"- {p} ({k}, mode {oct(m)})" for p, k, m in group[:50])
        more = f"\n... and {len(group) - 50} more" if len(group) > 50 else ""
        out.append(Finding("privesc", f"privesc.secret.loose.{sev}", sev, f"{len(group)} {title}",
                           why + " Contents were not read or printed; names are a heuristic, so some may be harmless.\n" + listing + more,
                           "Owner-only permissions: `chmod 600 PATH` for each (owner's yes first). Also set `umask 077` for the account that creates them, since the default 002 makes new files group-readable.",
                           {"count": len(group), "paths": [p for p, _, _ in group]}))
    if tight:
        listing = "\n".join(f"- {p} ({k}, mode {oct(m)})" for p, k, m in tight[:60])
        more = f"\n... and {len(tight) - 60} more" if len(tight) > 60 else ""
        out.append(Finding("privesc", "privesc.secret.inventory", "low", f"{len(tight)} secret-looking file(s) under your home, owner-only permissions",
                           "Permissions are fine, but these are where a stolen account or a leaky backup would find secrets. Values are not shown. Names are a heuristic, so some may be harmless.\n" + listing + more,
                           "Keep them out of backups, git and chat uploads; move long-lived tokens to a password manager or short-lived credentials where you can.",
                           {"count": len(tight)}))
    ssh = os.path.join(home, ".ssh")
    try:
        st = os.stat(ssh)
        if st.st_mode & 0o077:
            out.append(Finding("privesc", "privesc.ssh.dir", "med", f"The .ssh folder is open to other users (mode {oct(stat.S_IMODE(st.st_mode))})",
                               "SSH expects this folder to be owner-only.", "chmod 700 on the folder."))
    except OSError:
        pass
    return out


# ---- units under home -------------------------------------------------

def user_unit_findings(home: str) -> list[Finding]:
    unit_dir = os.path.join(home, ".config", "systemd", "user")
    if not os.path.isdir(unit_dir):
        return []
    try:
        names = [n for n in os.listdir(unit_dir) if n.endswith((".service", ".timer", ".socket"))]
    except OSError:
        return []
    if not names:
        return []
    return [Finding("privesc", "privesc.userunits", "low", f"{len(names)} systemd user unit(s) are writable by this account",
                    "Units: " + ", ".join(sorted(names)[:20]) + ". Anything running as this user can edit one to run code at every login. This is how user services normally work.",
                    "Keep the unit folder owner-only and review the units occasionally.", {"units": sorted(names)})]


# ---- optional real LinPEAS -------------------------------------------

def find_linpeas(home: str) -> str | None:
    for name in ("linpeas.sh", "linpeas"):
        hit = shutil.which(name)
        if hit:
            return hit
        for folder in (home, os.path.join(home, "tools"), "/opt", "/usr/local/bin"):
            candidate = os.path.join(folder, name)
            if os.path.isfile(candidate):
                return candidate
    return None


def linpeas_note(home: str, run_it: bool, timeout: float = 900) -> tuple[Finding | None, str]:
    path = find_linpeas(home)
    if not path:
        return None, "LinPEAS not installed, skipped (nothing is downloaded)."
    if not run_it:
        return None, f"LinPEAS found at {path} but disabled in config."
    code, out = run(["sh", path, "-q", "-N"], timeout=timeout)
    ansi = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
    clean = ansi.sub("", out)
    hits = [line.strip() for line in clean.splitlines() if re.search(r"\b(95%|99%|RED/YELLOW)\b", line)]
    detail = f"Ran the installed copy at {path} (exit {code}). {len(hits)} line(s) it marked as very likely. Output is not stored; run it yourself to read it."
    return Finding("privesc", "privesc.linpeas", "info", "Real LinPEAS ran locally", detail), f"LinPEAS ran ({path})."


def run_all(cfg: dict, home: str | None = None) -> tuple[list[Finding], list[str]]:
    home = home or str(Path.home())
    notes: list[str] = []
    findings: list[Finding] = []
    findings += check_sudo()
    findings += check_groups()
    findings += check_suid([str(r) for r in cfg["suid_roots"]], [str(x) for x in cfg["suid_known"]], [str(x) for x in cfg["suid_risky"]])
    path_dirs = list(dict.fromkeys(p for p in os.environ.get("PATH", "").split(":") if p))
    unit_dirs = [str(d).replace("$HOME", home) for d in cfg["unit_dirs"]]
    findings += check_writable(path_dirs, unit_dirs, home)
    prune = set(_DEFAULT_PRUNE) | {str(x) for x in cfg.get("secret_prune") or []}
    findings += check_secrets(home, prune, int(cfg["secret_max_depth"]))
    findings += user_unit_findings(home)
    peas, note = linpeas_note(home, bool(cfg.get("run_linpeas_if_present", True)))
    notes.append(note)
    if peas:
        findings.append(peas)
    return findings, notes
