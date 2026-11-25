//! Rust port of `sovereign_m.modelguard.cmdguard` (policy-review path only).
//!
//! Same policy files, same decision vocabulary (allow / ask / block), same
//! reason strings, same JSONL log schema. Python remains the live default;
//! this crate exists to be differentially tested against it.

pub mod pytables;
pub mod pytext;
pub mod yaml_lite;

use pytext::{as_list, lower, normalize_for_regex, py_str, split_ws, strip, truthy};
use regex::Regex;
use serde_json::{Map, Value};
use std::collections::BTreeSet;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

pub type R<T> = Result<T, String>;

fn re(p: &str) -> Regex {
    Regex::new(&format!("(?i){p}")).expect("builtin pattern")
}

static SPLIT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s*(?:&&|\|\||;|\n)\s*").unwrap());
static SHELL_PIPE: LazyLock<Regex> = LazyLock::new(|| re(r"\|\s*(?:sudo\s+)?(?:bash|sh|zsh|ksh|fish)\b"));
static SHELL_DASH_C: LazyLock<Regex> = LazyLock::new(|| re(r"\b(?:bash|sh|zsh)\s+(?:-c|--command)\b"));

static TRIGGER_CONTEXT: LazyLock<Vec<Regex>> = LazyLock::new(|| {
    vec![
        Regex::new(r"\b2027\b").unwrap(),
        Regex::new(r"\b2028\b").unwrap(),
        re(r"\brolled into 2027\b"),
        re(r"\bturn of the year\b"),
        re(r"\btraceback\b"),
        re(r"\bconnection refused\b"),
        re(r"\bpermission denied\b"),
        re(r"\bHTTP\s*500\b"),
        re(r"\bInternal Server Error\b"),
    ]
});

/// Names and order match payloads.py.
static PAYLOADS: LazyLock<Vec<(&'static str, Regex)>> = LazyLock::new(|| {
    vec![
        ("curl-or-wget-to-shell", re(r"\b(?:curl|wget)\b[\s\S]{0,400}\|\s*(?:sudo\s+)?(?:bash|sh|zsh)\b")),
        ("bash-dash-c", re(r"\b(?:bash|sh|zsh)\s+(?:-c|--command)\b")),
        ("rm-rf", re(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\b|\brm\s+-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*\b")),
        ("netcat", re(r"\b(?:nc|ncat|netcat)\b(?:\s+-\w+)*\s+")),
        ("base64-decode", re(r"\bbase64\s+(?:-d|--decode|-D)\b")),
        ("chmod-plus-x", re(r"\bchmod\s+(?:[0-7]*\+x|\+x|a\+x|u\+x)\b")),
        ("dev-tcp", re(r"/dev/tcp\b")),
        ("powershell-enc", re(r"\bpowershell(?:\.exe)?\s+(?:-enc|-encodedcommand)\b")),
        ("python-dash-c", re(r"\bpython(?:3(?:\.\d+)?)?\s+-c\b")),
        ("ssh", re(r"(?:^|[\s;|&])ssh\s+\S")),
        ("scp", re(r"(?:^|[\s;|&])scp\s+\S")),
    ]
});

pub fn find_payloads(text: &str) -> Vec<String> {
    if text.is_empty() {
        return vec![];
    }
    let t = normalize_for_regex(text);
    PAYLOADS.iter().filter(|(_, p)| p.is_match(&t)).map(|(n, _)| n.to_string()).collect()
}

fn strs(items: &[&str]) -> Value {
    Value::Array(items.iter().map(|s| Value::String(s.to_string())).collect())
}

pub fn default_policy() -> Value {
    let mut m = Map::new();
    m.insert("default".into(), "ask".into());
    m.insert("allow".into(), strs(&["ls", "pwd", "date", "whoami", "uname", "echo", "cat", "head", "wc", "true", "false"]));
    m.insert("ask".into(), strs(&[
        "rm", "mv", "cp", "chmod", "chown", "mkdir", "touch", "git", "pip", "npm", "apt", "dnf", "docker",
        "systemctl", "kill", "pkill", "reboot", "shutdown", "dd", "mount", "umount", "crontab", "python", "python3",
    ]));
    m.insert("network_tools".into(), strs(&["curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "ftp", "telnet", "rsh", "rclone"]));
    m.insert("startup_paths".into(), strs(&[
        "/etc/cron", "/etc/crontab", "/etc/systemd", "/etc/init.d", "/etc/rc.local", ".config/systemd", "crontab",
        ".bashrc", ".profile", ".zshrc", ".bash_profile",
    ]));
    m.insert("secret_paths".into(), strs(&[
        "/.ssh/", ".ssh/", "id_rsa", "id_ed25519", "authorized_keys", "known_hosts", "/etc/shadow", "/etc/sudoers",
        ".env", ".aws/credentials", ".gnupg/", "id_ecdsa",
    ]));
    m.insert("privilege_patterns".into(), strs(&[
        r"\bsudo\b", r"\bdoas\b", r"\bpkexec\b", r"\b(?:sudo\s+)?su\b", r"\bchmod\s+[0-7]*[4-6][0-7]{2,3}\b",
        r"\bchmod\s+[ug]\+s\b", r"\bchown\b", r"\busermod\b", r"\bpasswd\b", r"\bvisudo\b", r"\bsetcap\b",
    ]));
    Value::Object(m)
}

#[derive(Debug, Clone, PartialEq)]
pub struct Decision {
    pub action: String,
    pub command: String,
    pub reasons: Vec<String>,
    pub trigger_context: bool,
    pub payloads: Vec<String>,
}

impl Decision {
    pub fn to_value(&self) -> Value {
        let mut m = Map::new();
        m.insert("action".into(), self.action.clone().into());
        m.insert("command".into(), self.command.clone().into());
        m.insert("reasons".into(), self.reasons.clone().into());
        m.insert("trigger_context".into(), self.trigger_context.into());
        m.insert("payloads".into(), self.payloads.clone().into());
        Value::Object(m)
    }
}

pub fn format_decision(d: &Decision) -> String {
    let reasons = if d.reasons.is_empty() { "-".to_string() } else { d.reasons.join(",") };
    let flag = if d.trigger_context { "  trigger-context" } else { "" };
    format!("cmdguard {}  {}  {}{}", d.action.to_uppercase(), d.command, reasons, flag)
}

/// `merge` + `load_policy`: defaults, overlaid with a YAML file if given.
pub fn load_policy(path: Option<&Path>) -> R<Value> {
    let mut policy = default_policy();
    let source = match path {
        Some(p) => Some(p.to_path_buf()),
        None => default_policy_path(),
    };
    let Some(source) = source else { return Ok(policy) };
    let text = std::fs::read_to_string(&source).map_err(|e| format!("{}: {e}", source.display()))?;
    let loaded = yaml_lite::load(&text)?;
    let loaded = if truthy(&loaded) { loaded } else { Value::Object(Map::new()) };
    let Value::Object(incoming) = loaded else {
        return Err(format!("cmdguard policy must be a mapping: {}", source.display()));
    };
    merge(policy.as_object_mut().unwrap(), incoming);
    Ok(policy)
}

fn merge(base: &mut Map<String, Value>, incoming: Map<String, Value>) {
    for (key, value) in incoming {
        match (value, base.get_mut(&key)) {
            (Value::Object(v), Some(Value::Object(b))) => merge(b, v),
            (v, _) => {
                base.insert(key, v);
            }
        }
    }
}

/// Mirrors `paths.repo_root()/data/modelguard/cmdguard.yaml`, searching up
/// from the binary and then the working directory for `data/registry.json`.
pub fn default_policy_path() -> Option<PathBuf> {
    let mut starts: Vec<PathBuf> = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        starts.push(exe);
    }
    if let Ok(cwd) = std::env::current_dir() {
        starts.push(cwd);
    }
    for start in starts {
        for cand in start.ancestors() {
            if cand.join("data").join("registry.json").is_file() {
                let p = cand.join("data").join("modelguard").join("cmdguard.yaml");
                return if p.is_file() { Some(p) } else { None };
            }
        }
    }
    None
}

/// `review()`. `policy` None -> `load_policy(None)`.
pub fn review(command: &str, context: &str, policy: Option<&Value>, log_path: Option<&str>) -> R<Decision> {
    let text = strip(command).to_string();
    let loaded;
    let rules = match policy {
        Some(p) => p,
        None => {
            loaded = load_policy(None)?;
            &loaded
        }
    };
    let default_action = |rules: &Value| -> R<String> {
        match rules.get("default") {
            Some(v) if truthy(v) => py_str(v),
            _ => Ok("ask".to_string()),
        }
    };
    let mut reasons: Vec<String> = Vec::new();
    default_action(rules)?; // Python evaluates this eagerly; keep error parity
    let mut action: String;
    if text.is_empty() {
        action = "ask".into();
        reasons.push("empty".into());
    } else {
        let norm = normalize_for_regex(&text);
        let blocked = block_reasons(&text, &norm, rules)?;
        if !blocked.is_empty() {
            action = "block".into();
            reasons.extend(blocked);
        } else if allow_only(&text, &norm, rules)? {
            action = "allow".into();
            reasons.push("allowlist".into());
        } else if ask_match(&text, rules)? {
            action = "ask".into();
            reasons.push("high-impact".into());
        } else {
            action = default_action(rules)?;
            reasons.push("default".into());
        }
    }

    let payloads = find_payloads(&text);
    let trigger = trigger_context(context) && (!payloads.is_empty() || action != "allow");
    if trigger && action == "allow" {
        action = "ask".into();
        reasons.push("trigger-context".into());
    } else if trigger {
        reasons.push("trigger-context".into());
    }
    let decision = Decision { action, command: text, reasons, trigger_context: trigger, payloads };
    log_decision(&decision, context, log_path)?;
    Ok(decision)
}

fn block_reasons(text: &str, norm: &str, rules: &Value) -> R<Vec<String>> {
    let mut reasons = Vec::new();
    let verbs = verbs(text);
    let network: BTreeSet<String> = as_list(rules.get("network_tools"))?.iter().map(|s| lower(s)).collect();
    if verbs.iter().any(|v| network.contains(v)) {
        reasons.push("network".to_string());
    }
    if SHELL_PIPE.is_match(norm) || SHELL_DASH_C.is_match(norm) {
        reasons.push("shell-pipe".into());
    }
    if matches_any(text, &as_list(rules.get("startup_paths"))?) {
        reasons.push("startup-path".into());
    }
    if matches_any(text, &as_list(rules.get("secret_paths"))?) {
        reasons.push("secret-path".into());
    }
    for pattern in as_list(rules.get("privilege_patterns"))? {
        let compiled = cached_regex(&pattern)?;
        if compiled.is_match(norm) {
            reasons.push("privilege".into());
            break;
        }
    }
    let extra: BTreeSet<String> = as_list(rules.get("block"))?.iter().map(|s| lower(s)).collect();
    if verbs.iter().any(|v| extra.contains(v)) {
        reasons.push("blocklist".into());
    }
    Ok(reasons)
}

thread_local! {
    static PATTERN_CACHE: std::cell::RefCell<std::collections::HashMap<String, Regex>> = Default::default();
}

/// Compile (once) a policy privilege pattern, case-insensitive like `re.I`.
fn cached_regex(pattern: &str) -> R<Regex> {
    PATTERN_CACHE.with(|cache| {
        if let Some(r) = cache.borrow().get(pattern) {
            return Ok(r.clone());
        }
        let r = Regex::new(&format!("(?i){pattern}"))
            .map_err(|e| format!("unsupported: privilege pattern not accepted by Rust regex ({pattern:?}): {e}"))?;
        cache.borrow_mut().insert(pattern.to_string(), r.clone());
        Ok(r)
    })
}

fn allow_only(text: &str, norm: &str, rules: &Value) -> R<bool> {
    let allowed: BTreeSet<String> = as_list(rules.get("allow"))?.iter().map(|s| lower(s)).collect();
    let verbs = verbs(text);
    if verbs.is_empty() || allowed.is_empty() {
        return Ok(false);
    }
    if !verbs.is_subset(&allowed) {
        return Ok(false);
    }
    // A pipe to a shell must never be saved by the allowlist.
    if SHELL_PIPE.is_match(norm) || SHELL_DASH_C.is_match(norm) {
        return Ok(false);
    }
    Ok(true)
}

fn ask_match(text: &str, rules: &Value) -> R<bool> {
    let ask: BTreeSet<String> = as_list(rules.get("ask"))?.iter().map(|s| lower(s)).collect();
    Ok(verbs(text).iter().any(|v| ask.contains(v)))
}

fn verbs(text: &str) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for chunk in SPLIT.split(text) {
        let chunk = strip(chunk);
        if chunk.is_empty() {
            continue;
        }
        let mut parts = split_ws(chunk);
        while !parts.is_empty() && parts[0].contains('=') && !parts[0].starts_with('=') {
            parts.remove(0);
        }
        if parts.is_empty() {
            continue;
        }
        out.insert(command_name(parts[0]));
        for piece in chunk.split('|').skip(1) {
            let words = split_ws(strip(piece));
            if let Some(w) = words.first() {
                out.insert(command_name(w));
            }
        }
    }
    out
}

fn command_name(token: &str) -> String {
    let name = strip(token).trim_end_matches(':');
    let name = match name.rfind('/') {
        Some(i) => &name[i + 1..],
        None => name,
    };
    lower(name)
}

fn matches_any(text: &str, needles: &[String]) -> bool {
    let lowered = lower(text);
    needles.iter().any(|n| !n.is_empty() && lowered.contains(&lower(n)))
}

fn trigger_context(context: &str) -> bool {
    if context.is_empty() {
        return false;
    }
    let c = normalize_for_regex(context);
    TRIGGER_CONTEXT.iter().any(|p| p.is_match(&c))
}

// ---- JSONL log (same schema as Python) -----------------------------------

/// `json.dumps` string encoding with the default ensure_ascii=True.
pub fn json_str(s: &str) -> String {
    let mut out = String::from("\"");
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c if c.is_ascii() => out.push(c),
            c => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{:04x}", unit));
                }
            }
        }
    }
    out.push('"');
    out
}

fn json_list(items: &[String]) -> String {
    format!("[{}]", items.iter().map(|s| json_str(s)).collect::<Vec<_>>().join(", "))
}

pub fn utc_timestamp(secs: i64) -> String {
    let days = secs.div_euclid(86400);
    let rem = secs.rem_euclid(86400);
    // civil-from-days (Howard Hinnant)
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z.rem_euclid(146097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    format!("{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z", rem / 3600, rem % 3600 / 60, rem % 60)
}

pub fn log_row(d: &Decision, context: &str, ts: &str) -> String {
    let mut row = format!(
        "{{\"action\": {}, \"command\": {}, \"reasons\": {}, \"trigger_context\": {}, \"payloads\": {}, \"ts\": {}",
        json_str(&d.action),
        json_str(&d.command),
        json_list(&d.reasons),
        if d.trigger_context { "true" } else { "false" },
        json_list(&d.payloads),
        json_str(ts),
    );
    if !context.is_empty() {
        row.push_str(&format!(", \"context_chars\": {}", context.chars().count()));
    }
    row.push('}');
    row
}

fn log_decision(d: &Decision, context: &str, log_path: Option<&str>) -> R<()> {
    let env;
    let path = match log_path {
        Some(p) => p,
        None => {
            env = std::env::var("MODELGUARD_CMDGUARD_LOG").unwrap_or_default();
            strip(&env)
        }
    };
    if path.is_empty() {
        return Ok(());
    }
    let dest = Path::new(path);
    if let Some(parent) = dest.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent).map_err(|e| format!("log dir: {e}"))?;
        }
    }
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let line = log_row(d, context, &utc_timestamp(secs));
    let mut f = std::fs::OpenOptions::new().create(true).append(true).open(dest).map_err(|e| format!("log open: {e}"))?;
    writeln!(f, "{line}").map_err(|e| format!("log write: {e}"))?;
    Ok(())
}
