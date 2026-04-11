import json
from fnmatch import fnmatch
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ISSUE_RULES_PATH = PROJECT_ROOT / "templates" / "issue_rules.json"


def _load_issue_rules() -> list[dict]:
    if not ISSUE_RULES_PATH.is_file():
        return []
    try:
        data = json.loads(ISSUE_RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return sorted(
        [rule for rule in data if isinstance(rule, dict)],
        key=lambda rule: int(rule.get("priority", 100)),
    )


ISSUE_RULES = _load_issue_rules()


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


def _matches_any_glob(value: str, patterns: list[str]) -> bool:
    normalized = value.replace("\\", "/").lower()
    return any(fnmatch(normalized, pattern.lower()) for pattern in patterns)


def _contains_any(text: str, patterns: list[str]) -> bool:
    return any(pattern in text for pattern in patterns)


def _contains_all(text: str, patterns: list[str]) -> bool:
    return all(pattern in text for pattern in patterns)


def _override_severity(tool: str, rule_id: str, message: str, file_path: str, current: str) -> str:
    combined = "\n".join([(rule_id or "").lower(), (message or "").lower()])
    for rule in ISSUE_RULES:
        target_tool = (rule.get("tool") or "").strip().lower()
        if target_tool and target_tool != tool.lower():
            continue
        match_any = [token.lower() for token in rule.get("match_any", [])]
        match_all = [token.lower() for token in rule.get("match_all", [])]
        exclude_any = [token.lower() for token in rule.get("exclude_any", [])]
        message_match_any = [token.lower() for token in rule.get("message_match_any", [])]
        message_match_all = [token.lower() for token in rule.get("message_match_all", [])]
        file_match_any = rule.get("file_match_any", [])
        if match_any and not _contains_any(combined, match_any):
            continue
        if match_all and not _contains_all(combined, match_all):
            continue
        if exclude_any and _contains_any(combined, exclude_any):
            continue
        if message_match_any and not _contains_any((message or "").lower(), message_match_any):
            continue
        if message_match_all and not _contains_all((message or "").lower(), message_match_all):
            continue
        if file_match_any and not _matches_any_glob(file_path, file_match_any):
            continue
        severity = str(rule.get("severity", "")).upper()
        if severity in {"LOW", "MEDIUM", "HIGH"}:
            return severity
    return current


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
        severity = _override_severity("semgrep", check_id, message, path, severity)
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
            normalized_message = message.get("message") or ""
            normalized_severity = _map_severity(message.get("severity", 1))
            normalized_severity = _override_severity(
                "eslint",
                message.get("ruleId", "eslint"),
                normalized_message,
                file_path,
                normalized_severity,
            )
            row = {
                "tool": "eslint",
                "category": _map_category("eslint", message.get("ruleId", "eslint")),
                "severity": normalized_severity,
                "rule_id": message.get("ruleId") or "eslint",
                "file": file_path,
                "line": message.get("line") or 0,
                "message": normalized_message,
                "code": message.get("source") or "",
            }
            items.append(row)
    return items


def normalize_bandit(data: dict) -> list[dict]:
    items = []
    for result in data.get("results", []):
        message = result.get("issue_text") or ""
        severity = _map_severity(result.get("issue_severity") or "MEDIUM")
        severity = _override_severity(
            "bandit",
            result.get("test_id") or result.get("test_name") or "bandit",
            message,
            result.get("filename") or "",
            severity,
        )
        items.append(
            {
                "tool": "bandit",
                "category": _map_category("bandit", result.get("test_name", "bandit")),
                "severity": severity,
                "rule_id": result.get("test_id") or result.get("test_name") or "bandit",
                "file": result.get("filename") or "",
                "line": result.get("line_number") or 0,
                "message": message,
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
