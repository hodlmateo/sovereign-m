"""A tiny YAML subset: mappings, lists, scalars, and comments. No tags."""

from __future__ import annotations

from typing import Any


def load(text: str) -> Any:
    lines = _prep(text)
    value, index = _parse(lines, 0, 0)
    return value


def load_path(path) -> Any:
    return load(path.read_text(encoding="utf-8"))


def _prep(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for raw in text.splitlines():
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        out.append((indent, line.strip()))
    return out


def _strip_comment(line: str) -> str:
    in_single = False
    in_double = False
    for i, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return line[:i]
    return line


def _parse(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines):
        return None, index
    cur_indent, content = lines[index]
    if cur_indent < indent:
        return None, index
    if content.startswith("- "):
        return _list(lines, index, cur_indent)
    if ":" in content:
        return _mapping(lines, index, cur_indent)
    return _scalar(content), index + 1


def _mapping(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[dict, int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        cur_indent, content = lines[index]
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise ValueError(f"unexpected indent in mapping at: {content}")
        if content.startswith("- "):
            break
        if ":" not in content:
            raise ValueError(f"expected a key: {content}")
        key, rest = content.split(":", 1)
        key = key.strip()
        rest = rest.strip()
        index += 1
        if rest:
            result[key] = _scalar(rest)
            continue
        if index < len(lines) and lines[index][0] > indent:
            value, index = _parse(lines, index, lines[index][0])
            result[key] = value
        else:
            result[key] = None
    return result, index


def _list(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[list, int]:
    result: list[Any] = []
    while index < len(lines):
        cur_indent, content = lines[index]
        if cur_indent < indent:
            break
        if cur_indent != indent or not content.startswith("- "):
            break
        body = content[2:].strip()
        index += 1
        child_indent = indent + 2
        if not body:
            if index < len(lines) and lines[index][0] > indent:
                value, index = _parse(lines, index, lines[index][0])
                result.append(value)
            else:
                result.append(None)
            continue
        if ":" in body and not body.startswith(("'", '"')):
            key, rest = body.split(":", 1)
            item = {key.strip(): _scalar(rest.strip()) if rest.strip() else None}
            while index < len(lines) and lines[index][0] >= child_indent and not lines[index][1].startswith("- "):
                nested, index = _mapping(lines, index, lines[index][0])
                item.update(nested)
            result.append(item)
        else:
            result.append(_scalar(body))
    return result, index


def _scalar(text: str) -> Any:
    if text == "" or text in {"null", "Null", "~"}:
        return None
    if text in {"true", "True", "yes"}:
        return True
    if text in {"false", "False", "no"}:
        return False
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    if text.startswith("0") and text not in {"0"} and not text.startswith("0."):
        return text
    try:
        if any(char in text for char in ".eE") and text.replace(".", "", 1).replace("e", "", 1).replace("E", "", 1).replace("-", "", 1).replace("+", "", 1).isdigit():
            return float(text)
        return int(text)
    except ValueError:
        return text
