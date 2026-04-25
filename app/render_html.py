import json
import re
from fnmatch import fnmatch
from pathlib import Path
from markupsafe import Markup
from jinja2 import Environment, FileSystemLoader

try:
    from .llm_summary import generate_ai_summary, generate_api_test_summary, generate_fix_suggestions, translate_issue_messages
except ImportError:
    from llm_summary import generate_ai_summary, generate_api_test_summary, generate_fix_suggestions, translate_issue_messages

SKIPPED_COLLECTION_GLOBS = (
    "**/*integration*.collection.json",
    "**/*external*.collection.json",
    "**/*callback*.collection.json",
    "**/*webhook*.collection.json",
)
SKIP_REASON_RULES_FILE = ".dev-analyzer.skip-rules.json"


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
        "api_test": "API 테스트",
    }.get(category, category)


def _tool_label(tool: str) -> str:
    return {
        "semgrep": "Semgrep",
        "eslint": "ESLint",
        "bandit": "Bandit",
        "newman": "Newman",
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


def _build_api_test_summary(items: list[dict]) -> dict:
    api_items = [item for item in items if item.get("category") == "api_test"]
    failed = len(api_items)
    status_counts: dict[int, int] = {}
    slowest = None

    for item in api_items:
        status_code = int(item.get("status_code") or 0)
        if status_code:
            status_counts[status_code] = status_counts.get(status_code, 0) + 1

        response_time_ms = item.get("response_time_ms")
        if isinstance(response_time_ms, int):
            if slowest is None or response_time_ms > slowest.get("response_time_ms", 0):
                slowest = item

    return {
        "failed": failed,
        "status_counts": status_counts,
        "slowest": slowest,
    }


def _normalize_url_parts(value) -> list[str]:
    if isinstance(value, list):
        return [str(part) for part in value if part not in (None, "")]
    if value in (None, ""):
        return []
    return [str(value)]


def _format_query_string(query_items) -> str:
    if not isinstance(query_items, list):
        return ""
    parts = []
    for query in query_items:
        if not isinstance(query, dict):
            continue
        key = str(query.get("key") or "").strip()
        if not key:
            continue
        value = query.get("value")
        if value in (None, ""):
            parts.append(key)
        else:
            parts.append(f"{key}={value}")
    return "&".join(parts)


def _format_api_endpoint(request_url, include_query: bool = False) -> str:
    if isinstance(request_url, str):
        return request_url
    if isinstance(request_url, dict):
        raw = request_url.get("raw")
        if raw and include_query:
            return str(raw)
        protocol = str(request_url.get("protocol") or "").strip()
        host = _normalize_url_parts(request_url.get("host"))
        port = str(request_url.get("port") or "").strip()
        path = _normalize_url_parts(request_url.get("path"))
        query_text = _format_query_string(request_url.get("query"))
        host_text = ".".join(host)
        path_text = "/".join(path)
        base = host_text
        if base and port:
            base = f"{base}:{port}"
        if base and protocol:
            base = f"{protocol}://{base}"
        if base and path_text:
            endpoint = f"{base}/{path_text}"
        elif base:
            endpoint = base
        elif path_text:
            endpoint = f"/{path_text}"
        else:
            endpoint = ""
        if include_query and query_text:
            separator = "&" if "?" in endpoint else "?"
            return f"{endpoint}{separator}{query_text}" if endpoint else f"?{query_text}"
        if raw and not endpoint:
            return str(raw)
        if endpoint:
            return endpoint
    if isinstance(request_url, list):
        path_text = "/".join(str(part) for part in request_url if part)
        return f"/{path_text}" if path_text else ""
    return str(request_url or "")


def _format_header_lines(headers) -> str:
    if not isinstance(headers, list):
        return "-"
    lines = []
    for header in headers:
        if not isinstance(header, dict):
            continue
        key = str(header.get("key") or "").strip()
        value = str(header.get("value") or "").strip()
        if key:
            lines.append(f"{key}: {value}")
    return "\n".join(lines) if lines else "-"


def _decode_response_stream(stream) -> str:
    if not isinstance(stream, dict):
        return "-"
    if stream.get("type") != "Buffer":
        return "-"
    data = stream.get("data")
    if not isinstance(data, list):
        return "-"
    try:
        raw = bytes(int(value) for value in data)
    except (TypeError, ValueError):
        return "-"
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return "-"
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except (TypeError, ValueError, json.JSONDecodeError):
        return text


def _extract_request_body(request: dict) -> str:
    body = request.get("body") if isinstance(request.get("body"), dict) else {}
    raw = body.get("raw")
    if isinstance(raw, str) and raw.strip():
        raw = raw.strip()
        try:
            return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
        except (TypeError, ValueError, json.JSONDecodeError):
            return raw
    return "-"


def _build_request_target(request: dict) -> str:
    request_url = request.get("url")
    endpoint = _format_api_endpoint(request_url, include_query=True)
    return endpoint or "-"


def _api_match_key(method: str, endpoint: str) -> tuple[str, str]:
    method_key = str(method or "").upper().strip()
    endpoint_value = str(endpoint or "").strip()
    normalized = endpoint_value

    if "://" in normalized:
        _, _, remainder = normalized.partition("://")
        slash_index = remainder.find("/")
        normalized = remainder[slash_index:] if slash_index >= 0 else "/"
    elif normalized.startswith("{{") and "}}" in normalized:
        closing = normalized.find("}}")
        normalized = normalized[closing + 2 :] or "/"

    return method_key, normalized.strip()


def _normalize_route_path(path: str) -> str:
    normalized = str(path or "").strip()
    if not normalized:
        return "/"
    if "://" in normalized:
        _, _, remainder = normalized.partition("://")
        slash_index = remainder.find("/")
        normalized = remainder[slash_index:] if slash_index >= 0 else "/"
    elif normalized.startswith("{{") and "}}" in normalized:
        closing = normalized.find("}}")
        normalized = normalized[closing + 2 :] or "/"
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    normalized = re.sub(r"/{2,}", "/", normalized)
    return normalized.rstrip("/") or "/"


def _join_route_path(base_path: str, method_path: str) -> str:
    base = _normalize_route_path(base_path)
    sub = str(method_path or "").strip()
    if not sub:
        return base
    if sub.startswith("/"):
        return _normalize_route_path(sub)
    if base == "/":
        return _normalize_route_path(sub)
    return _normalize_route_path(f"{base}/{sub}")


def _discover_project_api_endpoints(repo_path: Path | None) -> list[dict]:
    if repo_path is None or not repo_path.exists():
        return []

    controller_files = sorted(repo_path.rglob("*.controller.ts"))
    controller_pattern = re.compile(r"@Controller\(\s*['\"]([^'\"]*)['\"]\s*\)")
    method_pattern = re.compile(r"@(Get|Post|Put|Patch|Delete|Options|Head)\(\s*(?:['\"]([^'\"]*)['\"])?\s*\)")

    discovered: dict[tuple[str, str], dict] = {}
    for controller_file in controller_files:
        try:
            text = controller_file.read_text(encoding="utf-8")
        except OSError:
            continue

        controller_match = controller_pattern.search(text)
        if not controller_match:
            continue
        base_path = controller_match.group(1)

        for method_match in method_pattern.finditer(text):
            method = method_match.group(1).upper()
            method_path = method_match.group(2) or ""
            endpoint = _join_route_path(base_path, method_path)
            key = (method, endpoint)
            if key in discovered:
                continue
            discovered[key] = {
                "method": method,
                "endpoint": endpoint,
                "source": str(controller_file.relative_to(repo_path)).replace("\\", "/"),
            }

    return sorted(discovered.values(), key=lambda item: (item["endpoint"], item["method"]))


def _build_api_endpoint_coverage(
    repo_path: Path | None,
    tests: list[dict],
) -> dict:
    discovered = _discover_project_api_endpoints(repo_path)
    discovered_keys = {
        _api_match_key(item.get("method", ""), item.get("endpoint", ""))
        for item in discovered
    }

    covered_keys = {
        _api_match_key(test.get("method", ""), test.get("endpoint", ""))
        for test in tests
        if test.get("method") and test.get("endpoint")
    }
    executed_keys = {
        _api_match_key(test.get("method", ""), test.get("endpoint", ""))
        for test in tests
        if test.get("result") in {"PASSED", "FAILED"} and test.get("method") and test.get("endpoint")
    }
    skipped_keys = {
        _api_match_key(test.get("method", ""), test.get("endpoint", ""))
        for test in tests
        if test.get("result") == "SKIPPED" and test.get("method") and test.get("endpoint")
    }

    if discovered_keys:
        covered_known = discovered_keys & covered_keys
        executed_known = discovered_keys & executed_keys
        skipped_known = discovered_keys & skipped_keys
    else:
        covered_known = covered_keys
        executed_known = executed_keys
        skipped_known = skipped_keys

    discovered_total = len(discovered_keys)
    covered_total = len(covered_known)
    executed_total = len(executed_known)
    skipped_total = len(skipped_known)
    coverage_rate = round((covered_total / discovered_total) * 100, 1) if discovered_total else 0

    return {
        "discovered_total": discovered_total,
        "covered_total": covered_total,
        "executed_total": executed_total,
        "skipped_total": skipped_total,
        "coverage_rate": coverage_rate,
    }


def _build_api_test_kind(
    test_name: str,
    status_code,
    result: str,
    message: str = "",
    suite_name: str = "",
) -> dict[str, str]:
    name = str(test_name or "").lower()
    status = str(status_code or "").strip()
    msg = str(message or "").lower()
    suite = str(suite_name or "").lower()
    combined = " ".join([name, msg, suite])

    if result == "SKIPPED":
        if any(token in combined for token in ["callback", "webhook", "interaction", "interactions", "oauth", "identity", "result"]):
            return {"label": "콜백/연동", "class": "integration"}
        if any(token in combined for token in ["trigger", "batch", "sync", "refresh", "rebuild", "publish"]):
            return {"label": "트리거/배치", "class": "trigger"}
        return {"label": "연동 제외", "class": "integration"}

    if "validation error" in combined or status == "400":
        return {"label": "검증 오류", "class": "validation"}
    if "auth error" in combined or status == "401":
        return {"label": "인증 오류", "class": "auth"}
    if any(token in combined for token in ["callback", "webhook", "oauth", "identity", "kakao"]):
        return {"label": "콜백/연동", "class": "integration"}
    if any(token in combined for token in ["trigger", "batch", "sync", "refresh", "rebuild", "publish"]):
        return {"label": "트리거/배치", "class": "trigger"}
    if any(token in combined for token in ["health", "docs"]):
        return {"label": "헬스체크", "class": "smoke"}
    if any(token in combined for token in ["page response", "returns array", "returns nickname", "availability", "search"]):
        return {"label": "조회", "class": "read"}
    return {"label": "스모크", "class": "smoke"}


def _build_api_test_role(test_name: str, request: dict | None = None) -> dict[str, str]:
    request = request or {}
    name = str(test_name or "").lower()
    headers = request.get("header") if isinstance(request.get("header"), list) else []
    auth_value = ""
    for header in headers:
        if not isinstance(header, dict):
            continue
        if str(header.get("key") or "").lower() != "authorization":
            continue
        auth_value = str(header.get("value") or "")
        break

    auth_lower = auth_value.lower()
    combined = " ".join([name, auth_lower]).strip()
    if "adminaccesstoken" in combined or "admin bearer token" in combined:
        return {"label": "Admin", "class": "admin"}
    if "lawyeraccesstoken" in combined or "lawyer bearer token" in combined:
        return {"label": "Lawyer", "class": "lawyer"}
    if "companyaccesstoken" in combined or "company bearer token" in combined:
        return {"label": "Company Manager", "class": "company"}
    return {"label": "Public", "class": "public"}


def _normalize_api_request_path(request_url: object) -> str:
    raw_url = request_url
    if isinstance(request_url, dict):
        raw_url = request_url.get("raw") or request_url.get("path") or ""
    if isinstance(raw_url, list):
        raw_url = "/" + "/".join(str(part).strip("/") for part in raw_url if str(part).strip("/"))

    text = str(raw_url or "").strip()
    if not text:
        return ""
    text = re.sub(r"^\{\{baseurl\}\}", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^https?://[^/]+", "", text, flags=re.IGNORECASE)
    text = text.split("?", 1)[0].strip()
    if not text:
        return ""
    if not text.startswith("/"):
        text = "/" + text
    return text


def _infer_authorization_expectation(test_name: str) -> str | None:
    name = str(test_name or "").lower()
    if any(token in name for token in ["returns forbidden", "returns unauthorized", "returns inaccessible"]):
        return "deny"
    if "bearer token" in name:
        return "allow"
    return None


def _lookup_authorization_expectation(matrix: dict, method: str, endpoint: str, role_label: str, test_name: str) -> str | None:
    routes = matrix.get("routes") if isinstance(matrix.get("routes"), dict) else {}
    route = routes.get(f"{method.upper()} {endpoint}") if isinstance(routes, dict) else None
    if isinstance(route, dict):
        roles = route.get("roles") if isinstance(route.get("roles"), dict) else {}
        role_entry = roles.get(role_label) if isinstance(roles, dict) else None
        if isinstance(role_entry, dict):
            expectations = role_entry.get("expectations") if isinstance(role_entry.get("expectations"), list) else []
            if "deny" in expectations:
                return "deny"
            if "allow" in expectations:
                return "allow"
    return _infer_authorization_expectation(test_name)


def _build_authorization_issue_message(test_name: str, request: dict, status_code: int, matrix: dict) -> str | None:
    role = _build_api_test_role(test_name, request)
    if role.get("label") == "Public":
        return None

    method = str(request.get("method") or "")
    endpoint = _normalize_api_request_path(request.get("url"))
    expectation = _lookup_authorization_expectation(matrix, method, endpoint, role.get("label", ""), test_name)
    if expectation is None:
        return None

    if expectation == "deny" and status_code < 400:
        return f"권한 기대 불일치: {role['label']} 권한은 {method} {endpoint} 호출이 차단되어야 하지만 실제 {status_code} 응답으로 허용되었습니다."
    if expectation == "allow" and status_code in {401, 403}:
        return f"권한 기대 불일치: {role['label']} 권한은 {method} {endpoint} 호출이 허용되어야 하지만 실제 {status_code} 응답으로 차단되었습니다."
    if expectation == "allow" and status_code == 404:
        return f"권한 기대 불일치: {role['label']} 권한은 {method} {endpoint} 호출이 허용되어야 하지만 실제 404 응답을 받았습니다. 권한 은닉 정책이 적용되었거나 시드 데이터 조건이 맞지 않는지 확인이 필요합니다."
    if expectation == "deny" and status_code >= 500:
        return f"권한 검증 실패: {role['label']} 권한은 {method} {endpoint} 호출이 차단되어야 하지만 차단 응답 전에 서버 오류({status_code})가 발생했습니다."
    return None


def _load_skip_reason_rules(repo_path: Path | None) -> list[dict]:
    if repo_path is None:
        return []

    rules_path = repo_path / SKIP_REASON_RULES_FILE
    if not rules_path.is_file():
        return []

    try:
        payload = json.loads(rules_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(payload, list):
        return []

    rules: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "").strip()
        if not reason:
            continue
        rules.append(
            {
                "method": str(item.get("method") or "").upper().strip(),
                "path_contains": str(item.get("path_contains") or "").strip().lower(),
                "path_pattern": str(item.get("path_pattern") or "").strip().lower(),
                "reason": reason,
            }
        )
    return rules


def _discover_skipped_collection_paths(repo_path: Path | None) -> list[Path]:
    if repo_path is None or not repo_path.exists():
        return []

    search_root = repo_path / "tests" / "postman"
    if not search_root.exists():
        search_root = repo_path

    discovered: list[Path] = []
    seen: set[Path] = set()
    for pattern in SKIPPED_COLLECTION_GLOBS:
        for path in search_root.glob(pattern):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            discovered.append(path)
    return sorted(discovered)


def _build_skipped_reason(
    endpoint: str,
    method: str,
    test_name: str = "",
    suite_name: str = "",
    skip_reason_rules: list[dict] | None = None,
) -> str:
    normalized = (endpoint or "").lower()
    method_upper = (method or "").upper()
    name_normalized = (test_name or "").lower()
    suite_normalized = (suite_name or "").lower()

    for rule in skip_reason_rules or []:
        rule_method = str(rule.get("method") or "").strip()
        if rule_method and rule_method != method_upper:
            continue

        path_contains = str(rule.get("path_contains") or "").strip()
        if path_contains and path_contains not in normalized:
            continue

        path_pattern = str(rule.get("path_pattern") or "").strip()
        if path_pattern and not fnmatch(normalized, path_pattern):
            continue

        return str(rule.get("reason") or "").strip()

    combined = " ".join([normalized, name_normalized, suite_normalized]).strip()
    if any(token in combined for token in ["callback", "webhook", "interaction", "interactions", "result"]):
        return "외부 시스템이 호출하는 콜백 또는 웹훅 성격의 API라서, 실제 서명이나 공급자 payload가 필요합니다. 기본 smoke 테스트에서는 이런 외부 호출을 재현하지 않아 별도 integration 테스트로 분리했습니다."
    if any(token in combined for token in ["oauth", "identity", "sso", "passport", "pass "]):
        return "외부 인증 또는 본인확인 연동 흐름이 필요한 API라서, 실제 인증 공급자 응답이나 콜백 데이터가 필요합니다. 기본 smoke 테스트에서는 해당 연동 단계를 재현하지 않아 별도 integration 테스트로 분리했습니다."
    if any(token in combined for token in ["trigger", "reindex", "refresh", "sync", "batch", "rebuild", "publish"]):
        return "트리거 또는 배치성 동작을 일으킬 수 있는 API라서, 실행 시 데이터 갱신이나 부작용이 생길 수 있습니다. 기본 smoke 테스트에서는 읽기 중심 검증만 수행하기 위해 별도 integration 테스트로 분리했습니다."
    if "integration" in suite_normalized or "external" in suite_normalized:
        return "외부 시스템 연동 성격의 API라서 추가 인증 정보, 서명, 콜백 payload 또는 부작용 검토가 필요합니다. 기본 smoke 테스트에서는 안정적인 검증만 수행하기 위해 별도 integration 테스트로 분리했습니다."

    return "기본 smoke 실행에서는 제외되었습니다. 외부 연동, 콜백, 트리거 성격이라 별도 integration 컬렉션에서 테스트하세요."



def _load_skipped_collection_tests(
    collection_path: Path,
    reason: str | None = None,
    skip_reason_rules: list[dict] | None = None,
) -> list[dict]:
    if not collection_path.is_file():
        return []

    try:
        payload = json.loads(collection_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    items = payload.get("item") if isinstance(payload.get("item"), list) else []
    tests: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        request = item.get("request") if isinstance(item.get("request"), dict) else {}
        request_url = request.get("url")
        test_name = str(item.get("name") or "Unnamed API Test").strip()
        method = str(request.get("method") or "")
        endpoint = _format_api_endpoint(request_url, include_query=False)
        tests.append(
            {
                "name": test_name,
                "method": method,
                "endpoint": endpoint,
                "status_code": "-",
                "response_time_ms": "-",
                "result": "SKIPPED",
                "result_label": "미실행",
                "result_class": "medium",
                "message": reason
                or _build_skipped_reason(
                    endpoint,
                    method,
                    test_name,
                    collection_path.name,
                    skip_reason_rules=skip_reason_rules,
                ),
                "suite": collection_path.name,
                "case_id": str(item.get("id") or test_name),
                "request_target": _build_request_target(request),
                "request_headers": _format_header_lines(request.get("header")),
                "request_body": _extract_request_body(request),
                "response_headers": "-",
                "response_body": "-",
            }
        )
        tests[-1]["kind"] = _build_api_test_kind(
            tests[-1]["name"],
            tests[-1]["status_code"],
            tests[-1]["result"],
            tests[-1]["message"],
            tests[-1]["suite"],
        )
        tests[-1]["role"] = _build_api_test_role(
            tests[-1]["name"],
            request,
        )
    return tests


def _build_api_tab_data(
    api_test_output: Path | None,
    repo_path: Path | None = None,
    skipped_collection_paths: list[Path] | None = None,
    skip_reason_rules: list[dict] | None = None,
) -> dict:
    default = {
        "available": False,
        "summary": {
            "total": 0,
            "test_case_total": 0,
            "executed": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "pass_rate": 0,
        },
        "tests": [],
        "groups": [],
        "endpoint_coverage": {
            "discovered_total": 0,
            "covered_total": 0,
            "executed_total": 0,
            "skipped_total": 0,
            "coverage_rate": 0,
        },
    }
    payload: dict = {}
    if api_test_output is not None and api_test_output.is_file():
        try:
            payload = json.loads(api_test_output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}

    report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
    run = report.get("run") if isinstance(report.get("run"), dict) else {}
    executions = run.get("executions") if isinstance(run.get("executions"), list) else []
    failures = run.get("failures") if isinstance(run.get("failures"), list) else []
    authorization_matrix = payload.get("authorization_matrix") if isinstance(payload.get("authorization_matrix"), dict) else {}

    failure_map: dict[str, list[str]] = {}
    for failure in failures:
        source = failure.get("source") if isinstance(failure.get("source"), dict) else {}
        error = failure.get("error") if isinstance(failure.get("error"), dict) else {}
        key = str(source.get("name") or "").strip()
        message = (
            error.get("test")
            or error.get("message")
            or ((failure.get("error") or {}).get("message") if isinstance(failure.get("error"), dict) else "")
            or failure.get("at")
            or "API 테스트 실패"
        )
        if key:
            failure_map.setdefault(key, []).append(str(message))

    tests = []
    passed = 0
    failed = 0
    for execution in executions:
        item = execution.get("item") if isinstance(execution.get("item"), dict) else {}
        request = execution.get("request") if isinstance(execution.get("request"), dict) else {}
        response = execution.get("response") if isinstance(execution.get("response"), dict) else {}
        request_url = request.get("url")

        test_name = str(item.get("name") or "Unnamed API Test").strip()
        failure_messages = list(failure_map.get(test_name, []))

        request_error = execution.get("requestError") if isinstance(execution.get("requestError"), dict) else {}
        request_error_message = str(request_error.get("message") or "").strip()
        if request_error_message and request_error_message not in failure_messages:
            failure_messages.insert(0, request_error_message)

        assertion_failures = execution.get("assertions") if isinstance(execution.get("assertions"), list) else []
        for assertion in assertion_failures:
            error = assertion.get("error") if isinstance(assertion, dict) else {}
            assertion_message = str((error or {}).get("message") or "").strip()
            if assertion_message and assertion_message not in failure_messages:
                failure_messages.append(assertion_message)

        result = "FAILED" if failure_messages else "PASSED"
        if result == "FAILED":
            failed += 1
        else:
            passed += 1

        status_code = response.get("code")
        if status_code in (None, ""):
            status_code = "-" if result == "FAILED" else 200

        response_time_ms = response.get("responseTime")
        if response_time_ms in (None, ""):
            response_time_ms = "-"

        tests.append(
            {
                "name": test_name,
                "method": str(request.get("method") or ""),
                "endpoint": _format_api_endpoint(request_url, include_query=False),
                "status_code": status_code,
                "response_time_ms": response_time_ms,
                "result": result,
                "result_label": "실패" if result == "FAILED" else "성공",
                "result_class": "high" if result == "FAILED" else "low",
                "message": " | ".join(failure_messages) if failure_messages else "성공",
                "suite": str(((execution.get("cursor") or {}).get("ref")) or ""),
                "case_id": str(((execution.get("cursor") or {}).get("httpRequestId")) or item.get("id") or test_name),
                "request_target": _build_request_target(request),
                "request_headers": _format_header_lines(request.get("header")),
                "request_body": _extract_request_body(request),
                "response_headers": _format_header_lines(response.get("header")),
                "response_body": _decode_response_stream(response.get("stream")),
            }
        )
        tests[-1]["kind"] = _build_api_test_kind(
            tests[-1]["name"],
            tests[-1]["status_code"],
            tests[-1]["result"],
            tests[-1]["message"],
            tests[-1]["suite"],
        )
        tests[-1]["role"] = _build_api_test_role(
            tests[-1]["name"],
            request,
        )
        if tests[-1]["result"] == "FAILED":
            auth_issue_message = _build_authorization_issue_message(
                tests[-1]["name"],
                request,
                int(status_code) if isinstance(status_code, int) or str(status_code).isdigit() else 0,
                authorization_matrix,
            )
            if auth_issue_message:
                tests[-1]["message"] = f"{auth_issue_message} | 원본: {tests[-1]['message']}"

    skipped_tests: list[dict] = []
    for collection_path in skipped_collection_paths or []:
        skipped_tests.extend(
            _load_skipped_collection_tests(
                collection_path,
                skip_reason_rules=skip_reason_rules,
            )
        )

    skipped_keys = {
        _api_match_key(test.get("method", ""), test.get("endpoint", ""))
        for test in skipped_tests
    }
    if skipped_keys:
        tests = [
            test
            for test in tests
            if _api_match_key(test.get("method", ""), test.get("endpoint", "")) not in skipped_keys
        ]

    tests.extend(skipped_tests)

    passed = sum(1 for test in tests if test.get("result") == "PASSED")
    failed = sum(1 for test in tests if test.get("result") == "FAILED")
    executed_total = passed + failed
    skipped = len(skipped_tests)
    total = len(tests)
    pass_rate = round((passed / executed_total) * 100, 1) if executed_total else 0

    grouped: dict[tuple[str, str], dict] = {}
    for test in tests:
        group_key = (test.get("method", ""), test.get("endpoint", ""))
        group = grouped.setdefault(
            group_key,
            {
                "name": test.get("name", ""),
                "method": test.get("method", ""),
                "endpoint": test.get("endpoint", ""),
                "total": 0,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "cases": [],
            },
        )
        group["total"] += 1
        if test.get("result") == "FAILED":
            group["failed"] += 1
        elif test.get("result") == "SKIPPED":
            group["skipped"] += 1
        else:
            group["passed"] += 1
        group["cases"].append(test)

    groups = sorted(
        grouped.values(),
        key=lambda item: (
            item["failed"] == 0 and item["skipped"] == 0,
            item["method"],
            item["endpoint"],
            item["name"],
        ),
    )

    for group in groups:
        labels = []
        seen = set()
        for case in group.get("cases", []):
            kind = case.get("kind") or {}
            label = str(kind.get("label") or "").strip()
            if label and label not in seen:
                seen.add(label)
                labels.append(label)
        group["kind_labels"] = labels

    passed_groups = [group for group in groups if group.get("failed", 0) == 0 and group.get("skipped", 0) == 0]
    failed_groups = [group for group in groups if group.get("failed", 0) > 0]
    skipped_groups = [group for group in groups if group.get("failed", 0) == 0 and group.get("skipped", 0) > 0]
    endpoint_coverage = _build_api_endpoint_coverage(repo_path, tests)

    return {
        "available": bool(tests),
        "summary": {
            "total": total,
            "test_case_total": total,
            "executed": executed_total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "pass_rate": pass_rate,
        },
        "tests": tests,
        "groups": groups,
        "endpoint_coverage": endpoint_coverage,
        "group_layers": {
            "passed": passed_groups,
            "failed": failed_groups,
            "skipped": skipped_groups,
        },
        "layer_summary": {
            "passed": {
                "endpoint_count": len(passed_groups),
                "case_count": passed,
            },
            "failed": {
                "endpoint_count": len(failed_groups),
                "case_count": failed,
            },
            "skipped": {
                "endpoint_count": len(skipped_groups),
                "case_count": skipped,
            },
        },
    }
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


def render_report(
    items: list[dict],
    output_path: Path,
    template_dir: Path,
    tool_outputs: dict[str, Path] | None = None,
    report_context: dict | None = None,
) -> Path:
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
    api_test_items = [item for item in sorted_items if item.get("category") == "api_test"]
    static_items = [item for item in items if item.get("category") != "api_test"]
    static_issues = [item for item in top_issues if item.get("category") != "api_test"]
    ai_fix_targets: list[dict] = []
    for item in static_issues:
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

    summary = _build_summary(static_items)
    api_test_summary = _build_api_test_summary(items)
    repo_path_raw = (report_context or {}).get("repo_path")
    repo_path = Path(repo_path_raw) if repo_path_raw else None
    skipped_collections = _discover_skipped_collection_paths(repo_path)
    skip_reason_rules = _load_skip_reason_rules(repo_path)
    api_tab = _build_api_tab_data(
        (tool_outputs or {}).get("api_test"),
        repo_path=repo_path,
        skipped_collection_paths=skipped_collections,
        skip_reason_rules=skip_reason_rules,
    )
    api_reason_messages = [
        str(test.get("message", "")).strip()
        for test in api_tab.get("tests", [])
        if test.get("result") in {"FAILED", "SKIPPED"} and str(test.get("message", "")).strip()
    ]
    api_reason_translations = translate_issue_messages(
        api_reason_messages,
        cache_path=output_path.parent / "api_reason_translations_cache.json",
    )
    for test in api_tab.get("tests", []):
        raw_reason = str(test.get("message", "")).strip()
        if test.get("result") in {"FAILED", "SKIPPED"} and raw_reason:
            test["translated_reason"] = api_reason_translations.get(raw_reason, raw_reason)
        else:
            test["translated_reason"] = ""
    all_group_sets: list[list[dict]] = []
    if isinstance(api_tab.get("groups"), list):
        all_group_sets.append(api_tab.get("groups", []))
    group_layers = api_tab.get("group_layers")
    if isinstance(group_layers, dict):
        for groups in group_layers.values():
            if isinstance(groups, list):
                all_group_sets.append(groups)

    for groups in all_group_sets:
        for group in groups:
            translated_reasons = []
            for case in group.get("cases", []):
                raw_reason = str(case.get("message", "")).strip()
                if case.get("result") in {"FAILED", "SKIPPED"} and raw_reason:
                    case["translated_reason"] = api_reason_translations.get(raw_reason, raw_reason)
                else:
                    case["translated_reason"] = ""
                if case["translated_reason"]:
                    translated_reasons.append(case["translated_reason"])
            group["translated_reason"] = translated_reasons[0] if translated_reasons else ""
    summary["severity_labels"] = {
        "HIGH": _severity_label("HIGH"),
        "MEDIUM": _severity_label("MEDIUM"),
        "LOW": _severity_label("LOW"),
    }
    summary["tool_labels"] = {
        tool: _tool_label(tool) for tool in summary["tool"].keys()
    }
    rule_groups = _build_rule_groups(static_items)
    for group in rule_groups:
        message = (group.get("message") or "").strip()
        group["translated_message"] = translations.get(message, message)
    recommendations = _build_recommendations(rule_groups)
    active_targets = set((report_context or {}).get("analysis_targets", []))
    show_static_tab = "static" in active_targets if active_targets else True
    show_api_tab = "api" in active_targets if active_targets else True
    default_tab = "api-tests" if show_api_tab and not show_static_tab else "static-analysis"
    data = {
        "summary": summary,
        "issues": static_issues,
        "static_issues": static_issues,
        "api_test_issues": api_test_items[:20],
        "api_test_summary": api_test_summary,
        "api_tab": api_tab,
        "api_test_status": (report_context or {}).get("api_test_status", {}),
        "rule_groups": rule_groups[:20],
        "recommendations": recommendations[:10],
        "ai_summary_html": Markup(generate_ai_summary(static_issues, summary)),
        "api_ai_summary_html": Markup(generate_api_test_summary(api_tab)),
        "show_static_tab": show_static_tab,
        "show_api_tab": show_api_tab,
        "default_tab": default_tab,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(template.render(data=data), encoding="utf-8")
    return output_path
