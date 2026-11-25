//! Faithful port of sovereign_m/modelguard/yaml_lite.py (mappings, lists,
//! scalars, comments). Values that Python would parse in ways Rust cannot
//! reproduce exactly (Unicode digits, ints beyond i64, odd floats) return
//! Err so callers fall back to Python rather than diverge.

use crate::pytext::{splitlines, strip};
use serde_json::{Map, Number, Value};

type Lines = Vec<(usize, String)>;
type R<T> = Result<T, String>;

pub fn load(text: &str) -> R<Value> {
    let lines = prep(text);
    let (value, _) = parse(&lines, 0, 0)?;
    Ok(value)
}

fn prep(text: &str) -> Lines {
    let mut out = Vec::new();
    for raw in splitlines(text) {
        let stripped = strip_comment(raw);
        let line = stripped.trim_end_matches(crate::pytext::is_space);
        if strip(line).is_empty() {
            continue;
        }
        let indent = line.chars().count() - line.trim_start_matches(' ').chars().count();
        out.push((indent, strip(line).to_string()));
    }
    out
}

fn strip_comment(line: &str) -> &str {
    let (mut single, mut double) = (false, false);
    for (i, c) in line.char_indices() {
        if c == '\'' && !double {
            single = !single;
        } else if c == '"' && !single {
            double = !double;
        } else if c == '#' && !single && !double {
            return &line[..i];
        }
    }
    line
}

fn parse(lines: &Lines, index: usize, indent: usize) -> R<(Value, usize)> {
    if index >= lines.len() {
        return Ok((Value::Null, index));
    }
    let (cur, content) = (&lines[index].0, &lines[index].1);
    if *cur < indent {
        return Ok((Value::Null, index));
    }
    if content.starts_with("- ") {
        return list(lines, index, *cur);
    }
    if content.contains(':') {
        return mapping(lines, index, *cur);
    }
    Ok((scalar(content)?, index + 1))
}

fn mapping(lines: &Lines, mut index: usize, indent: usize) -> R<(Value, usize)> {
    let mut result = Map::new();
    while index < lines.len() {
        let (cur, content) = (lines[index].0, &lines[index].1);
        if cur < indent {
            break;
        }
        if cur > indent {
            return Err(format!("unexpected indent in mapping at: {content}"));
        }
        if content.starts_with("- ") {
            break;
        }
        let Some((key, rest)) = content.split_once(':') else {
            return Err(format!("expected a key: {content}"));
        };
        let key = strip(key).to_string();
        let rest = strip(rest);
        index += 1;
        if !rest.is_empty() {
            result.insert(key, scalar(rest)?);
            continue;
        }
        if index < lines.len() && lines[index].0 > indent {
            let (value, next) = parse(lines, index, lines[index].0)?;
            index = next;
            result.insert(key, value);
        } else {
            result.insert(key, Value::Null);
        }
    }
    Ok((Value::Object(result), index))
}

fn list(lines: &Lines, mut index: usize, indent: usize) -> R<(Value, usize)> {
    let mut result = Vec::new();
    while index < lines.len() {
        let (cur, content) = (lines[index].0, &lines[index].1);
        if cur < indent {
            break;
        }
        if cur != indent || !content.starts_with("- ") {
            break;
        }
        let body = strip(&content[2..]).to_string();
        index += 1;
        let child_indent = indent + 2;
        if body.is_empty() {
            if index < lines.len() && lines[index].0 > indent {
                let (value, next) = parse(lines, index, lines[index].0)?;
                index = next;
                result.push(value);
            } else {
                result.push(Value::Null);
            }
            continue;
        }
        if body.contains(':') && !body.starts_with(['\'', '"']) {
            let (key, rest) = body.split_once(':').unwrap();
            let rest = strip(rest);
            let mut item = Map::new();
            item.insert(
                strip(key).to_string(),
                if rest.is_empty() { Value::Null } else { scalar(rest)? },
            );
            while index < lines.len()
                && lines[index].0 >= child_indent
                && !lines[index].1.starts_with("- ")
            {
                let (nested, next) = mapping(lines, index, lines[index].0)?;
                index = next;
                if let Value::Object(m) = nested {
                    for (k, v) in m {
                        item.insert(k, v);
                    }
                }
            }
            result.push(Value::Object(item));
        } else {
            result.push(scalar(&body)?);
        }
    }
    Ok((Value::Array(result), index))
}

fn scalar(text: &str) -> R<Value> {
    if text.is_empty() || matches!(text, "null" | "Null" | "~") {
        return Ok(Value::Null);
    }
    if matches!(text, "true" | "True" | "yes") {
        return Ok(Value::Bool(true));
    }
    if matches!(text, "false" | "False" | "no") {
        return Ok(Value::Bool(false));
    }
    let chars: Vec<char> = text.chars().collect();
    if chars.len() >= 2 && chars[0] == chars[chars.len() - 1] && (chars[0] == '\'' || chars[0] == '"') {
        let body: String = chars[1..chars.len() - 1].iter().collect();
        return Ok(Value::String(if chars[0] == '"' { unescape_double(&body) } else { body }));
    }
    if text.starts_with('0') && text != "0" && !text.starts_with("0.") {
        return Ok(Value::String(text.to_string()));
    }
    // Python: float branch, else int(), either failing -> the raw text.
    let stripped = text
        .replacen('.', "", 1)
        .replacen('e', "", 1)
        .replacen('E', "", 1)
        .replacen('-', "", 1)
        .replacen('+', "", 1);
    if text.contains(['.', 'e', 'E']) && !stripped.is_empty() && stripped.chars().all(|c| c.is_ascii_digit()) {
        return match text.parse::<f64>() {
            Ok(f) => Number::from_f64(f)
                .map(Value::Number)
                .ok_or_else(|| format!("unsupported: non-finite float scalar: {text}")),
            Err(_) => Ok(Value::String(text.to_string())),
        };
    }
    parse_int(text)
}

/// Python `int(text)` for text, else the text itself.
fn parse_int(text: &str) -> R<Value> {
    let (sign, digits) = match text.strip_prefix('-') {
        Some(r) => ("-", r),
        None => ("", text.strip_prefix('+').unwrap_or(text)),
    };
    let ascii_ok = !digits.is_empty()
        && digits.chars().all(|c| c.is_ascii_digit() || c == '_')
        && !digits.starts_with('_')
        && !digits.ends_with('_')
        && !digits.contains("__");
    if ascii_ok {
        let clean: String = digits.chars().filter(|c| *c != '_').collect();
        return match format!("{sign}{clean}").parse::<i64>() {
            Ok(i) => Ok(Value::Number(i.into())),
            Err(_) => Err(format!("unsupported: integer scalar out of range: {text}")),
        };
    }
    // Python also accepts non-ASCII Unicode digits; refuse to guess.
    if digits.chars().all(|c| c == '_' || c.is_numeric()) && digits.chars().any(|c| !c.is_ascii() && c.is_numeric()) {
        return Err(format!("unsupported: non-ASCII numeric scalar: {text}"));
    }
    Ok(Value::String(text.to_string()))
}

fn unescape_double(body: &str) -> String {
    let chars: Vec<char> = body.chars().collect();
    let mut out = String::new();
    let mut i = 0;
    while i < chars.len() {
        if chars[i] == '\\' && i + 1 < chars.len() {
            let nxt = chars[i + 1];
            out.push(match nxt {
                'n' => '\n',
                't' => '\t',
                other => other, // '\\' and '"' map to themselves; unknown -> char
            });
            i += 2;
        } else {
            out.push(chars[i]);
            i += 1;
        }
    }
    out
}
