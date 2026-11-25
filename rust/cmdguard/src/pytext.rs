//! Python `str` / `re` semantics that differ from Rust's, ported on purpose.
//!
//! cmdguard is a security control: a command Python blocks must never be
//! allowed here because Rust's whitespace, word-character or case-folding
//! rules differ. Everything here exists to close such gaps.

use crate::pytables::{CASE_MAP, PY_ONLY_WORD, RUST_ONLY_WORD};
use serde_json::Value;

/// Python `str.isspace` (== regex `\s` for str patterns).
pub fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// Python `str.strip()`.
pub fn strip(s: &str) -> &str {
    s.trim_matches(is_space)
}

/// Python `str.split()` with no arguments.
pub fn split_ws(s: &str) -> Vec<&str> {
    s.split(is_space).filter(|p| !p.is_empty()).collect()
}

/// Python `str.splitlines()`.
pub fn splitlines(s: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0usize;
    let mut iter = s.char_indices().peekable();
    while let Some((i, c)) = iter.next() {
        let is_break = matches!(
            c,
            '\n' | '\r' | '\u{0b}' | '\u{0c}' | '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{85}' | '\u{2028}' | '\u{2029}'
        );
        if is_break {
            out.push(&s[start..i]);
            let mut end = i + c.len_utf8();
            if c == '\r' {
                if let Some(&(j, '\n')) = iter.peek() {
                    iter.next();
                    end = j + 1;
                }
            }
            start = end;
        }
    }
    if start < s.len() {
        out.push(&s[start..]);
    }
    out
}

fn in_ranges(table: &[(u32, u32)], cp: u32) -> bool {
    table
        .binary_search_by(|&(lo, hi)| {
            if cp < lo {
                std::cmp::Ordering::Greater
            } else if cp > hi {
                std::cmp::Ordering::Less
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}

/// Rewrite text so Rust's `regex` crate classifies it the way Python's `re`
/// does (re.I folding, `\w`/`\b`, `\s`). Length in chars is preserved, so
/// bounded repeats like `{0,400}` behave identically.
pub fn normalize_for_regex(s: &str) -> String {
    s.chars()
        .map(|c| {
            if c.is_ascii() {
                // Python's `\s` also matches the four ASCII separators 0x1c-0x1f. Use EM SPACE:
                // Rust's `\s` matches it, but a literal ' ' in a pattern does not (as in Python).
                return if ('\u{1c}'..='\u{1f}').contains(&c) { '\u{2003}' } else { c };
            }
            let cp = c as u32;
            if let Ok(i) = CASE_MAP.binary_search_by_key(&cp, |&(k, _)| k) {
                return CASE_MAP[i].1;
            }
            if in_ranges(PY_ONLY_WORD, cp) {
                return '\u{00aa}'; // word char in both engines, no case relatives
            }
            if in_ranges(RUST_ONLY_WORD, cp) {
                return '\u{2022}'; // neither word nor space in both engines
            }
            c
        })
        .collect()
}

/// Python truthiness of a JSON-ish value (as yaml_lite would have produced).
pub fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Python `str(value)` for the scalar types a policy can reasonably hold.
/// Containers return Err so the caller fails closed (or falls back to Python)
/// instead of guessing at `repr`.
pub fn py_str(v: &Value) -> Result<String, String> {
    match v {
        Value::Null => Ok("None".into()),
        Value::Bool(true) => Ok("True".into()),
        Value::Bool(false) => Ok("False".into()),
        Value::String(s) => Ok(s.clone()),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Ok(i.to_string())
            } else if let Some(f) = n.as_f64() {
                let a = f.abs();
                if f == 0.0 || (1e-4..1e16).contains(&a) {
                    Ok(format!("{:?}", f))
                } else {
                    Err(format!("unsupported: float repr: {f}"))
                }
            } else {
                Err("unsupported: number".into())
            }
        }
        Value::Array(_) | Value::Object(_) => Err("unsupported: nested container in policy scalar position".into()),
    }
}

/// `_as_list` from cmdguard.py.
pub fn as_list(v: Option<&Value>) -> Result<Vec<String>, String> {
    match v {
        None | Some(Value::Null) => Ok(vec![]),
        Some(Value::Array(items)) => items
            .iter()
            .filter(|i| !i.is_null())
            .map(py_str)
            .collect(),
        Some(other) => Ok(vec![py_str(other)?]),
    }
}

/// Python `str.lower()`.
pub fn lower(s: &str) -> String {
    s.to_lowercase()
}
