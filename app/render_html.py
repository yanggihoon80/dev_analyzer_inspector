import json
from fnmatch import fnmatch
from pathlib import Path
from markupsafe import Markup
from jinja2 import Environment, FileSystemLoader

try:
    from .llm_summary import generate_ai_summary, generate_fix_suggestions, translate_issue_messages
except ImportError:
    from llm_summary import generate_ai_summary, generate_fix_suggestions, translate_issue_messages


def _severity_value(severity: str) -> int:
    return {"HIGH": 3, "MEDIUM": 2, "LOW": 1}.get(severity.upper(), 1)


def _severity_label(severity: str) -> str:
    return {
        "HIGH": "높음",
        "MEDIUM": "보통",
        "LOW": "낮음",
    }.get(severity.upper(), severity)


def _category_label(category: str) -> str:
    return {
        "security": "보안",
        "code_quality": "코드 품질",
    }.get(category, category)


def _tool_label(tool: str) -> str:
    return {
        "semgrep": "Semgrep",
        "eslint": "ESLint",
        "bandit": "Bandit",
        "unknown": "알 수 없음",
    }.get(tool, tool)


def _load_issue_rules(template_dir: Path) -> list[dict]:
    rules_path = template_dir / "issue_rules.json"
    if not rules_path.is_file():
        return []
    try:
        data = json.loads(rules_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return sorted(
        [rule for rule in data if isinstance(rule, dict)],
        key=lambda rule: int(rule.get("priority", 100)),
    )


def _decorate_item(item: dict) -> dict:
    item["severity_label"] = _severity_label(item.get("severity", "MEDIUM"))
    item["category_label"] = _category_label(item.get("category", "code_quality"))
    item["tool_label"] = _tool_label(item.get("tool", "unknown"))
    item["translated_message"] = item.get("translated_message") or item.get("message", "")
    return item


def _apply_translations(items: list[dict], translations: dict[str, str]) -> None:
    for item in items:
        message = (item.get("message") or "").strip()
        item["translated_message"] = translations.get(message, message)


def _build_summary(items: list[dict]) -> dict:
    summary = {
        "total": len(items),
        "severity": {"HIGH": 0, "MEDIUM": 0, "LOW": 0},
        "tool": {},
        "most_affected_file": None,
        "top_risky_issue": None,
        "duplicate_rules": [],
    }
    rule_counts: dict[str, int] = {}
    file_counts: dict[str, int] = {}
    for item in items:
        severity = item.get("severity", "MEDIUM")
        summary["severity"][severity] = summary["severity"].get(severity, 0) + 1

        tool = item.get("tool", "unknown")
        summary["tool"][tool] = summary["tool"].get(tool, 0) + 1

        rule_id = item.get("rule_id", "unknown")
        rule_counts[rule_id] = rule_counts.get(rule_id, 0) + 1

        file_name = item.get("file", "unknown")
        file_counts[file_name] = file_counts.get(file_name, 0) + 1

    if file_counts:
        most_file = max(file_counts.items(), key=lambda pair: pair[1])
        summary["most_affected_file"] = {"file": most_file[0], "count": most_file[1]}

    duplicate_rules = [
        {"rule_id": rule, "count": count}
        for rule, count in sorted(rule_counts.items(), key=lambda pair: pair[1], reverse=True)
        if count > 1
    ]
    summary["duplicate_rules"] = duplicate_rules[:10]

    if items:
        sorted_issues = sorted(
            items,
            key=lambda item: (
                _severity_value(item.get("severity", "MEDIUM")),
                item.get("category", "code_quality") == "security",
                rule_counts.get(item.get("rule_id", ""), 0),
            ),
            reverse=True,
        )
        top = sorted_issues[0]
        summary["top_risky_issue"] = {
            "rule_id": top.get("rule_id", ""),
            "severity": top.get("severity", "MEDIUM"),
            "severity_label": _severity_label(top.get("severity", "MEDIUM")),
            "file": top.get("file", ""),
            "line": top.get("line", 0),
            "message": top.get("message", ""),
            "translated_message": top.get("translated_message", top.get("message", "")),
            "tool": top.get("tool", ""),
            "tool_label": _tool_label(top.get("tool", "")),
        }

    return summary


def _build_rule_groups(items: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for item in items:
        rule_id = item.get("rule_id", "unknown")
        group = groups.setdefault(
            rule_id,
            {
                "rule_id": rule_id,
                "category": item.get("category", "code_quality"),
                "severity": item.get("severity", "MEDIUM"),
                "severity_value": _severity_value(item.get("severity", "MEDIUM")),
                "count": 0,
                "files": set(),
                "tools": set(),
                "message": item.get("message", ""),
            },
        )
        group["count"] += 1
        group["files"].add(item.get("file", "unknown"))
        group["tools"].add(item.get("tool", "unknown"))
        severity_value = _severity_value(item.get("severity", "MEDIUM"))
        if severity_value > group["severity_value"]:
            group["severity_value"] = severity_value
            group["severity"] = item.get("severity", "MEDIUM")
            group["message"] = item.get("message", "")
        if severity_value == group["severity_value"] and not group["message"]:
            group["message"] = item.get("message", "")

    results = []
    for group in groups.values():
        results.append(
            {
                "rule_id": group["rule_id"],
                "category": group["category"],
                "category_label": _category_label(group["category"]),
                "severity": group["severity"],
                "severity_label": _severity_label(group["severity"]),
                "count": group["count"],
                "affected_files": len(group["files"]),
                "files": sorted(group["files"]),
                "tools": sorted(group["tools"]),
                "tool_labels": [_tool_label(tool) for tool in sorted(group["tools"])],
                "message": group["message"],
                "translated_message": group.get("translated_message", group["message"]),
                "severity_value": group["severity_value"],
            }
        )
    return sorted(
        results,
        key=lambda group: (
            group["category"] != "security",
            -group["affected_files"],
            -group["severity_value"],
            -group["count"],
        ),
    )


def _build_recommendations(groups: list[dict]) -> list[dict]:
    recommendations = []
    for index, group in enumerate(groups, start=1):
        reasons: list[str] = []
        if group["category"] == "security":
            reasons.append("보안 관련 이슈")
        if group["affected_files"] > 1:
            reasons.append(f"{group['affected_files']}개 파일 영향")
        if group["count"] > 1:
            reasons.append(f"{group['count']}개의 중복 이슈")
        if not reasons:
            reasons.append("우선 해결해야 할 높은 우선순위")
        recommendations.append(
            {
                "rank": index,
                "rule_id": group["rule_id"],
                "category": group["category"],
                "category_label": _category_label(group["category"]),
                "severity": group["severity"],
                "severity_label": _severity_label(group["severity"]),
                "count": group["count"],
                "affected_files": group["affected_files"],
                "files": group["files"],
                "tools": group["tools"],
                "tool_labels": [_tool_label(tool) for tool in group["tools"]],
                "message": group["message"],
                "translated_message": group.get("translated_message", group["message"]),
                "reason": "; ".join(reasons),
            }
        )
    return recommendations


def _fallback_fix_suggestion(item: dict) -> dict:
    code_context = item.get("code", "").strip()
    return {
        "title": "수동 검토가 필요합니다",
        "why_risky": "이 이슈에 대한 안전한 수정 패턴을 검토하고 적용하세요.",
        "recommended_fix": "이 발견 항목을 수동으로 검토하고 보안 또는 코드 품질 기준에 맞게 수정하십시오.",
        "before_example": code_context or "실제 코드가 없으면 여기에 사용된 패턴을 확인하세요.",
        "after_example": "안전한 코딩 패턴을 적용한 수정 코드를 작성하세요.",
    }


def _issue_signature(item: dict) -> str:
    from hashlib import sha256
    import json

    payload = json.dumps(
        {
            "rule_id": item.get("rule_id", ""),
            "message": item.get("message", ""),
            "file": item.get("file", ""),
            "line": item.get("line", 0),
            "code": item.get("code", ""),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _resolve_template_placeholders(text: str, item: dict, code_context: str) -> str:
    if not text:
        return text
    replacements = {
        "{code_context}": code_context or "실제 코드가 없으면 여기에 사용된 패턴을 확인하세요.",
        "{message}": item.get("translated_message") or item.get("message", ""),
        "{raw_message}": item.get("message", ""),
        "{file}": item.get("file", ""),
        "{line}": str(item.get("line", 0)),
        "{rule_id}": item.get("rule_id", ""),
        "{severity}": item.get("severity_label") or item.get("severity", ""),
        "{tool}": item.get("tool_label") or item.get("tool", ""),
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def _contains_any(text: str, patterns: list[str]) -> bool:
    return any(pattern in text for pattern in patterns)


def _contains_all(text: str, patterns: list[str]) -> bool:
    return all(pattern in text for pattern in patterns)


def _matches_any_glob(value: str, patterns: list[str]) -> bool:
    normalized = value.replace("\\", "/").lower()
    return any(fnmatch(normalized, pattern.lower()) for pattern in patterns)


def _is_rule_match(item: dict, rule: dict) -> bool:
    rule_id = (item.get("rule_id", "") or "").lower()
    message = (item.get("message", "") or "").lower()
    translated_message = (item.get("translated_message", "") or "").lower()
    file_path = item.get("file", "") or ""
    combined = "\n".join([rule_id, message, translated_message])

    match_any = [token.lower() for token in rule.get("match_any", [])]
    match_all = [token.lower() for token in rule.get("match_all", [])]
    exclude_any = [token.lower() for token in rule.get("exclude_any", [])]
    message_match_any = [token.lower() for token in rule.get("message_match_any", [])]
    message_match_all = [token.lower() for token in rule.get("message_match_all", [])]
    file_match_any = rule.get("file_match_any", [])
    file_match_all = rule.get("file_match_all", [])

    if match_any and not _contains_any(combined, match_any):
        return False
    if match_all and not _contains_all(combined, match_all):
        return False
    if exclude_any and _contains_any(combined, exclude_any):
        return False
    if message_match_any and not _contains_any(message, message_match_any):
        return False
    if message_match_all and not _contains_all(message, message_match_all):
        return False
    if file_match_any and not _matches_any_glob(file_path, file_match_any):
        return False
    if file_match_all and not all(fnmatch(file_path.replace("\\", "/").lower(), pattern.lower()) for pattern in file_match_all):
        return False
    return True


def _match_fix_suggestion_rule(item: dict, rules: list[dict]) -> dict | None:
    code_context = item.get("code", "").strip()
    for rule in rules:
        if not _is_rule_match(item, rule):
            continue
        suggestion = rule.get("fix_suggestion")
        if not isinstance(suggestion, dict):
            continue
        return {
            "title": _resolve_template_placeholders(suggestion.get("title", ""), item, code_context),
            "why_risky": _resolve_template_placeholders(suggestion.get("why_risky", ""), item, code_context),
            "recommended_fix": _resolve_template_placeholders(suggestion.get("recommended_fix", ""), item, code_context),
            "before_example": _resolve_template_placeholders(suggestion.get("before_example", ""), item, code_context),
            "after_example": _resolve_template_placeholders(suggestion.get("after_example", ""), item, code_context),
        }
    return None


def render_report(items: list[dict], output_path: Path, template_dir: Path) -> Path:
    environment = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=True,
    )
    template = environment.get_template("report.html.j2")
    issue_rules = _load_issue_rules(template_dir)

    severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    sorted_items = sorted(
        items,
        key=lambda item: (
            severity_order.get(item.get("severity", "MEDIUM"), 1),
            item.get("category", "code_quality") != "security",
            item.get("file", ""),
            item.get("line", 0),
        ),
    )
    translations = translate_issue_messages(
        [item.get("message", "") for item in items],
        cache_path=output_path.parent / "message_translations_cache.json",
    )
    _apply_translations(items, translations)
    top_issues = sorted_items[:50]
    ai_fix_targets: list[dict] = []
    for item in top_issues:
        _decorate_item(item)
        fix_suggestion = _match_fix_suggestion_rule(item, issue_rules)
        if fix_suggestion is not None:
            item["fix_suggestion"] = fix_suggestion
        else:
            ai_fix_targets.append(item)

    ai_fix_suggestions = generate_fix_suggestions(
        ai_fix_targets,
        cache_path=output_path.parent / "fix_suggestions_cache.json",
    )
    for item in ai_fix_targets:
        suggestion = ai_fix_suggestions.get(_issue_signature(item), {})
        if suggestion and all(suggestion.get(field) for field in ["title", "why_risky", "recommended_fix", "before_example", "after_example"]):
            item["fix_suggestion"] = suggestion
        else:
            item["fix_suggestion"] = _fallback_fix_suggestion(item)

    summary = _build_summary(items)
    summary["severity_labels"] = {
        "HIGH": _severity_label("HIGH"),
        "MEDIUM": _severity_label("MEDIUM"),
        "LOW": _severity_label("LOW"),
    }
    summary["tool_labels"] = {
        tool: _tool_label(tool) for tool in summary["tool"].keys()
    }
    rule_groups = _build_rule_groups(items)
    for group in rule_groups:
        message = (group.get("message") or "").strip()
        group["translated_message"] = translations.get(message, message)
    recommendations = _build_recommendations(rule_groups)
    data = {
        "summary": summary,
        "issues": top_issues,
        "rule_groups": rule_groups[:20],
        "recommendations": recommendations[:10],
        "ai_summary_html": Markup(generate_ai_summary(top_issues, summary)),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(template.render(data=data), encoding="utf-8")
    return output_path
