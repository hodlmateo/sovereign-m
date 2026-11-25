//! cmdguard-rs: CLI identical to `python -m sovereign_m.modelguard cmdguard`,
//! plus three machine modes used by the Python shim and the test suite.

use cmdguard::{format_decision, load_policy, review, Decision};
use serde_json::{Map, Value};
use std::io::{BufRead, Read, Write};
use std::path::PathBuf;

const USAGE: &str = "usage: modelguard cmdguard [--context FILE] [--log FILE] [--policy FILE] -- COMMAND";

fn main() {
    let args: Vec<String> = match std::env::args_os().skip(1).map(|a| a.into_string()).collect() {
        Ok(a) => a,
        Err(_) => {
            eprintln!("cmdguard-rs: non-UTF-8 argument");
            std::process::exit(2);
        }
    };
    let code = match args.first().map(String::as_str) {
        Some("--rs-stdin-json") => machine_single(),
        Some("--rs-batch") => machine_batch(),
        Some("--rs-dump-word") => dump_word(),
        _ => cli(&args),
    };
    std::process::exit(code);
}

fn cli(args: &[String]) -> i32 {
    let (mut context_path, mut log_path, mut policy_path): (Option<String>, Option<String>, Option<String>) = (None, None, None);
    let mut parts: Vec<String> = Vec::new();
    let mut i = 0;
    while i < args.len() {
        let arg = &args[i];
        if arg == "--" {
            parts.extend(args[i + 1..].iter().cloned());
            break;
        }
        if arg == "--context" || arg == "--log" || arg == "--policy" {
            if i + 1 >= args.len() {
                println!("{USAGE}");
                return 2;
            }
            let v = Some(args[i + 1].clone());
            match arg.as_str() {
                "--context" => context_path = v,
                "--log" => log_path = v,
                _ => policy_path = v,
            }
            i += 2;
            continue;
        }
        if arg.starts_with('-') {
            println!("{USAGE}");
            return 2;
        }
        parts.push(arg.clone());
        i += 1;
    }
    let command = cmdguard::pytext::strip(&parts.join(" ")).to_string();
    if command.is_empty() {
        println!("{USAGE}");
        return 2;
    }
    let run = || -> Result<Decision, String> {
        let context = match &context_path {
            Some(p) => std::fs::read_to_string(p).map_err(|e| format!("{p}: {e}"))?,
            None => String::new(),
        };
        let policy = match &policy_path {
            Some(p) => Some(load_policy(Some(&PathBuf::from(p)))?),
            None => None,
        };
        review(&command, &context, policy.as_ref(), log_path.as_deref())
    };
    match run() {
        Ok(d) => {
            println!("{}", format_decision(&d));
            if d.action == "allow" { 0 } else { 1 }
        }
        Err(e) => {
            // Python would raise (exit 1); never report success on an error.
            eprintln!("cmdguard-rs: {e}");
            1
        }
    }
}

fn handle(req: &Value) -> Result<Value, String> {
    let s = |k: &str| req.get(k).and_then(Value::as_str);
    if let Some(path) = s("load_policy") {
        return load_policy(Some(&PathBuf::from(path)));
    }
    let command = s("command").ok_or("missing command")?;
    let context = s("context").unwrap_or("");
    let policy = match (req.get("policy"), s("policy_path")) {
        (Some(p), _) if !p.is_null() => Some(p.clone()),
        (_, Some(path)) => Some(load_policy(Some(&PathBuf::from(path)))?),
        _ => None,
    };
    let d = review(command, context, policy.as_ref(), s("log_path"))?;
    Ok(d.to_value())
}

fn machine_single() -> i32 {
    let mut buf = String::new();
    if std::io::stdin().read_to_string(&mut buf).is_err() {
        return 1;
    }
    let out = serde_json::from_str::<Value>(&buf).map_err(|e| e.to_string()).and_then(|r| handle(&r));
    match out {
        Ok(v) => {
            println!("{v}");
            0
        }
        Err(e) => {
            eprintln!("cmdguard-rs: {e}");
            1
        }
    }
}

fn machine_batch() -> i32 {
    let stdout = std::io::stdout();
    let mut w = std::io::BufWriter::new(stdout.lock());
    for line in std::io::stdin().lock().lines() {
        let Ok(line) = line else { return 1 };
        let out = match serde_json::from_str::<Value>(&line).map_err(|e| e.to_string()).and_then(|r| handle(&r)) {
            Ok(v) => v,
            Err(e) => {
                let mut m = Map::new();
                m.insert("error".into(), e.into());
                Value::Object(m)
            }
        };
        let _ = writeln!(w, "{out}");
    }
    0
}

/// Print `lo hi` (hex) for every run of code points the regex crate calls `\w`.
fn dump_word() -> i32 {
    let re = regex::Regex::new(r"\A\w\z").unwrap();
    let mut start: Option<u32> = None;
    let mut prev = 0u32;
    let mut buf = [0u8; 4];
    for cp in 0..=0x10FFFFu32 {
        let hit = char::from_u32(cp).map(|c| re.is_match(c.encode_utf8(&mut buf))).unwrap_or(false);
        match (hit, start) {
            (true, None) => start = Some(cp),
            (false, Some(s)) => {
                println!("{s:x} {prev:x}");
                start = None;
            }
            _ => {}
        }
        prev = cp;
    }
    if let Some(s) = start {
        println!("{s:x} {prev:x}");
    }
    0
}
