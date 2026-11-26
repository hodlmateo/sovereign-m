"""Reading only. Never executes anything from the repo and never follows symlinks.
Matched secret values are never stored: a finding keeps a kind, a path and a line number."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_FILES = 3000
MAX_BYTES = 1_000_000
SRC_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cc", ".cpp", ".h", ".rb", ".sh", ".php", ".cs", ".kt", ".swift"}
SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", "__pycache__", ".venv", "venv", "third_party"}

CURL_BASH = re.compile(r"\b(curl|wget)\b[^\n|;]*\|\s*(sudo\s+)?(ba|z|da)?sh\b", re.IGNORECASE)
OBFUSCATION = (
    re.compile(r"\b(exec|eval)\s*\(\s*(base64\.)?b64decode\s*\(", re.IGNORECASE),
    re.compile(r"\b(exec|eval)\s*\(\s*(zlib|bz2|lzma)\.decompress\s*\(", re.IGNORECASE),
    re.compile(r"marshal\.loads\s*\("),
    re.compile(r"(\\x[0-9a-fA-F]{2}){60,}"),
    re.compile(r"[A-Za-z0-9+/=]{4000,}"),
)
SECRETS = (
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github token", re.compile(r"\b(ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{50,})\b")),
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("slack token", re.compile(r"\bxox[abp]-[A-Za-z0-9-]{20,}\b")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)
INJECTION = re.compile(
    r"ignore (all |any )?(previous|prior|above|earlier) (instructions|prompts)|disregard (all |any )?(previous|prior|above) |"
    r"<\|im_start\|>|reveal (your|the) (instructions|prompt|keys?)", re.IGNORECASE)
LINT_FILES = {"ruff.toml", ".ruff.toml", ".flake8", "mypy.ini", ".pre-commit-config.yaml", "tox.ini", ".eslintrc", ".eslintrc.json",
              ".eslintrc.js", "tsconfig.json", "biome.json", ".golangci.yml", "clippy.toml", "rustfmt.toml", "pyrightconfig.json"}
LICENSE_FILES = ("license", "licence", "copying", "unlicense")


@dataclass
class Scan:
    files: int = 0
    readme_chars: int = 0
    readme_has_usage: bool = False
    doc_signals: int = 0
    test_files: int = 0
    py_test_files: int = 0
    ci: bool = False
    license_file: bool = False
    lint_config: bool = False
    src_lines: int = 0
    test_lines: int = 0
    biggest_src_lines: int = 0
    has_pyproject: bool = False
    has_requirements: bool = False
    has_package_json: bool = False
    languages: dict = field(default_factory=dict)
    flags: list = field(default_factory=list)          # [{"kind", "where"}]
    injection_seen: bool = False
    readme_text: str = ""

    def flag(self, kind: str, where: str) -> None:
        self.flags.append({"kind": kind, "where": where})


def _is_text(data: bytes) -> bool:
    return b"\0" not in data[:4096]


def _read(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    return data.decode("utf-8", errors="replace") if _is_text(data) else None


def scan_repo(root: Path) -> Scan:
    s = Scan()
    root = Path(root)
    md_files = 0
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d))]
        rel_dir = Path(dirpath).relative_to(root)
        low_dir = {p.lower() for p in rel_dir.parts}
        if low_dir & {"docs", "doc", "examples", "example"}:
            s.doc_signals += 1
        for fn in files:
            if s.files >= MAX_FILES:
                s.flag("scan truncated", f"stopped at {MAX_FILES} files")
                return s
            p = Path(dirpath) / fn
            if p.is_symlink() or not p.is_file():
                continue
            s.files += 1
            rel = str(p.relative_to(root))
            low = fn.lower()
            ext = p.suffix.lower()
            top = len(rel_dir.parts) == 0
            if top and low.startswith("readme"):
                txt = _read(p) or ""
                s.readme_chars = max(s.readme_chars, len(txt))
                if len(txt) >= len(s.readme_text):
                    s.readme_text = txt[:6000]
                if re.search(r"^#+\s*(install|installation|usage|quick ?start|getting started|setup)\b", txt, re.IGNORECASE | re.MULTILINE):
                    s.readme_has_usage = True
            if top and low.split(".")[0] in LICENSE_FILES:
                s.license_file = True
            if low in LINT_FILES:
                s.lint_config = True
            if top and low == "pyproject.toml":
                s.has_pyproject = True
            if top and low.startswith("requirements") and low.endswith(".txt"):
                s.has_requirements = True
            if top and low == "package.json":
                s.has_package_json = True
            if ".github/workflows" in rel.replace(os.sep, "/") or low in (".gitlab-ci.yml", ".travis.yml", "azure-pipelines.yml", "jenkinsfile") \
                    or rel.replace(os.sep, "/").startswith(".circleci/"):
                s.ci = True
            if ext == ".md":
                md_files += 1
            is_test = bool({"test", "tests", "__tests__", "spec"} & low_dir) or low.startswith("test_") or low.endswith(("_test.py", "_test.go", ".test.js", ".test.ts", ".spec.js", ".spec.ts"))
            text = _read(p) if (ext in SRC_EXT or ext in (".md", ".txt", ".toml", ".json", ".yml", ".yaml", ".cfg", ".sh", "") or low == "setup.py") else None
            if text is None:
                continue
            lines = text.count("\n") + 1
            if ext in SRC_EXT:
                s.languages[ext] = s.languages.get(ext, 0) + lines
                if is_test:
                    s.test_files += 1
                    s.py_test_files += 1 if ext == ".py" else 0
                    s.test_lines += lines
                else:
                    s.src_lines += lines
                    s.biggest_src_lines = max(s.biggest_src_lines, lines)
            elif is_test and ext in (".md",):
                pass
            _security(s, rel, low, ext, text)
    if md_files >= 3:
        s.doc_signals += 1
    return s


def _security(s: Scan, rel: str, low: str, ext: str, text: str) -> None:
    if CURL_BASH.search(text):
        s.flag("curl|bash pipe", rel)
    if low == "package.json":
        try:
            scripts = (json.loads(text).get("scripts") or {})
        except ValueError:
            scripts = {}
        for hook in ("preinstall", "install", "postinstall"):
            if hook in scripts:
                s.flag(f"npm {hook} script", rel)
    if low == "setup.py" and re.search(r"cmdclass|os\.system\(|subprocess\.|urlopen\(|requests\.(get|post)\(", text):
        s.flag("setup.py runs code at install", rel)
    if ext in SRC_EXT or low == "setup.py":
        for pat in OBFUSCATION:
            m = pat.search(text)
            if m and not (low.endswith(".min.js") or ".min." in low):
                s.flag("obfuscated code", rel)
                break
        if any(len(line) > 5000 for line in text.splitlines()) and ".min." not in low:
            s.flag("obfuscated code", rel)
    for kind, pat in SECRETS:
        for m in pat.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            s.flag(f"secret: {kind}", f"{rel}:{line}")        # value is NOT stored
            break
    if (low.startswith("readme") or ext == ".md") and INJECTION.search(text):
        s.injection_seen = True
        s.flag("prompt-injection text", rel)
    # de-dup identical flags per file
    seen, out = set(), []
    for f in s.flags:
        key = (f["kind"], f["where"])
        if key not in seen:
            seen.add(key)
            out.append(f)
    s.flags = out
