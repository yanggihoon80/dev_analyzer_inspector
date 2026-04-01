import json
from pathlib import Path
from typing import Any


def _read_code_excerpt(repo_path: Path, file_path: str, line: int, context_lines: int = 3) -> str:
    normalized = file_path.replace("\\", "/").lstrip("./").lstrip("/")
    path = repo_path / Path(normalized)
    if not path.is_file():
        return ""

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""

    if line <= 0 or line > len(lines):
        return ""

    start = max(1, line - context_lines)
    end = min(len(lines), line + context_lines)
    snippet_lines = []
    for i in range(start, end + 1):
        prefix = ">" if i == line else " "
        snippet_lines.append(f"{prefix} {i}: {lines[i - 1]}")
    return "\n".join(snippet_lines)


def _map_severity(value: Any) -> str:
    if isinstance(value, str):
        normalized = value.strip().upper()
        if normalized in {"LOW", "MEDIUM", "HIGH"}:
            return normalized
        if normalized.isdigit():
            return _map_severity(int(normalized))
    if isinstance(value, int):
        if value >= 2:
            return "HIGH"
        if value == 1:
            return "MEDIUM"
        return "LOW"
    return "MEDIUM"


def _map_category(tool: str, rule_id: str) -> str:
    if tool == "bandit":
        return "security"
    if tool == "eslint":
        return "code_quality"
    if tool == "semgrep":
        lower_rule = rule_id.lower()
        if any(keyword in lower_rule for keyword in ["security", "sqli", "xss", "ssrf", "injection", "hardcoded"]):
            return "security"
        return "code_quality"
    return "code_quality"


def _load_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else {}


def normalize_semgrep(data: dict) -> list[dict]:
    items = []
    for result in data.get("results", []):
        check_id = result.get("check_id") or result.get("checkid") or "semgrep"
        path = result.get("path") or result.get("extra", {}).get("metadata", {}).get("path", "")
        line = result.get("start", {}).get("line") or result.get("extra", {}).get("line") or 0
        extra = result.get("extra", {})
        message = extra.get("message") or result.get("message") or ""
        code = extra.get("lines") or ""
        severity = _map_severity(extra.get("metadata", {}).get("severity") or result.get("severity") or "MEDIUM")
        items.append(
            {
                "tool": "semgrep",
                "category": _map_category("semgrep", check_id),
                "severity": severity,
                "rule_id": check_id,
                "file": path,
                "line": line,
                "message": message,
                "code": code,
            }
        )
    return items


def normalize_eslint(data: list[dict]) -> list[dict]:
    items = []
    for entry in data:
        file_path = entry.get("filePath") or ""
        for message in entry.get("messages", []):
            row = {
                "tool": "eslint",
                "category": _map_category("eslint", message.get("ruleId", "eslint")),
                "severity": _map_severity(message.get("severity", 1)),
                "rule_id": message.get("ruleId") or "eslint",
                "file": file_path,
                "line": message.get("line") or 0,
                "message": message.get("message") or "",
                "code": message.get("source") or "",
            }
            items.append(row)
    return items


def normalize_bandit(data: dict) -> list[dict]:
    items = []
    for result in data.get("results", []):
        items.append(
            {
                "tool": "bandit",
                "category": _map_category("bandit", result.get("test_name", "bandit")),
                "severity": _map_severity(result.get("issue_severity") or "MEDIUM"),
                "rule_id": result.get("test_id") or result.get("test_name") or "bandit",
                "file": result.get("filename") or "",
                "line": result.get("line_number") or 0,
                "message": result.get("issue_text") or "",
                "code": result.get("code") or "",
            }
        )
    return items


def merge_results(tool_outputs: dict[str, Path], output_path: Path, repo_path: Path) -> list[dict]:
    all_items = []
    for tool_name, path in tool_outputs.items():
        raw = _load_json(path)
        if tool_name == "semgrep":
            items = normalize_semgrep(raw)
        elif tool_name == "eslint":
            items = normalize_eslint(raw)
        elif tool_name == "bandit":
            items = normalize_bandit(raw)
        else:
            items = []
        all_items.extend(items)

    for item in all_items:
        code = item.get("code") or ""
        if not code.strip() or code.strip().lower() in {"requires login", "no code", "unknown", "n/a"}:
            excerpt = _read_code_excerpt(repo_path, item.get("file", ""), item.get("line", 0))
            if excerpt:
                item["code"] = excerpt
            elif not code.strip():
                item["code"] = "코드 정보 없음"
            else:
                item["code"] = "코드 스니펫을 찾을 수 없습니다. 원본 도구 출력이 코드 정보를 제공하지 않았습니다."

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(all_items, indent=2), encoding="utf-8")
    return all_items
