from pathlib import Path
from markupsafe import Markup
from jinja2 import Environment, FileSystemLoader

try:
    from .llm_summary import generate_ai_summary
except ImportError:
    from llm_summary import generate_ai_summary


def _severity_value(severity: str) -> int:
    return {"HIGH": 3, "MEDIUM": 2, "LOW": 1}.get(severity.upper(), 1)


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
            "file": top.get("file", ""),
            "line": top.get("line", 0),
            "message": top.get("message", ""),
            "tool": top.get("tool", ""),
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
                "severity": group["severity"],
                "count": group["count"],
                "affected_files": len(group["files"]),
                "files": sorted(group["files"]),
                "tools": sorted(group["tools"]),
                "message": group["message"],
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
                "severity": group["severity"],
                "count": group["count"],
                "affected_files": group["affected_files"],
                "files": group["files"],
                "tools": group["tools"],
                "message": group["message"],
                "reason": "; ".join(reasons),
            }
        )
    return recommendations


def _build_fix_suggestion(item: dict) -> dict:
    rule_id = item.get("rule_id", "").lower()
    code_context = item.get("code", "").strip()
    fallback = {
        "title": "수동 검토가 필요합니다",
        "why_risky": "이 이슈에 대한 안전한 수정 패턴을 검토하고 적용하세요.",
        "recommended_fix": "이 발견 항목을 수동으로 검토하고 보안 또는 코드 품질 기준에 맞게 수정하십시오.",
        "before_example": code_context or "실제 코드가 없으면 여기에 사용된 패턴을 확인하세요.",
        "after_example": "안전한 코딩 패턴을 적용한 수정 코드를 작성하세요.",
    }

    if "wildcard-postmessage-configuration" in rule_id or "wildcard-postmessage" in rule_id:
        return {
            "title": "postMessage에 명시적 origin 사용",
            "why_risky": "'*'를 사용하면 모든 origin이 메시지를 받을 수 있어 민감 데이터가 노출될 수 있습니다.",
            "recommended_fix": "신뢰할 수 있는 origin 값을 명시적으로 지정하고, 가능한 경우 상수 또는 설정으로 분리하세요.",
            "before_example": code_context or 'window.opener.postMessage(data, "*")',
            "after_example": 'window.opener.postMessage(data, "https://trusted.example.com")',
        }

    if "detected-generic-api-key" in rule_id or "generic-api-key" in rule_id:
        return {
            "title": "비밀 정보는 하드코딩하지 마세요",
            "why_risky": "코드에 포함된 API 키는 저장소, 로그, 번들, 스크린샷을 통해 쉽게 유출될 수 있습니다.",
            "recommended_fix": "민감한 키는 환경 변수 또는 서버 보관소로 이동하고, 프런트엔드 번들에는 노출하지 마세요.",
            "before_example": code_context or 'apiKey="hardcoded-secret"',
            "after_example": 'apiKey={process.env.NEXT_PUBLIC_EDITOR_API_KEY}',
        }

    if "detect-non-literal-regexp" in rule_id or "non-literal-regexp" in rule_id:
        return {
            "title": "사용자 입력 RegExp를 안전하게 처리",
            "why_risky": "비리터럴 RegExp는 사용자 입력에 의해 생성될 때 ReDoS를 일으킬 수 있습니다.",
            "recommended_fix": "사용자 입력을 먼저 escape하거나 정적 매칭을 사용하며, 가능한 경우 RegExp 생성자를 직접 호출하지 마세요.",
            "before_example": code_context or 'new RegExp(term + ",?", "g")',
            "after_example": 'new RegExp(escapeRegExp(term) + ",?", "g")\n\nfunction escapeRegExp(value) {\n  return value.replace(/[.*+?^${}()|[\\]\\]/g, "\\$&");\n}',
        }

    return fallback


def render_report(items: list[dict], output_path: Path, template_dir: Path) -> Path:
    environment = Environment(loader=FileSystemLoader(str(template_dir)))
    template = environment.get_template("report.html.j2")

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
    top_issues = sorted_items[:50]
    for item in top_issues:
        item["fix_suggestion"] = _build_fix_suggestion(item)

    summary = _build_summary(items)
    rule_groups = _build_rule_groups(items)
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
