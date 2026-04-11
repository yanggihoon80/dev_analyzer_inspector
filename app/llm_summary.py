import html
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _get_openai_key() -> str | None:
    return os.getenv("OPENAI_API_KEY")


def _build_prompt(items: list[dict], summary: dict) -> str:
    top_items = items[:10]
    lines = [
        "한국어로 답변하세요.",
        "결과는 보고서에 바로 삽입할 수 있는 HTML 조각(fragment)으로만 작성하세요.",
        "HTML 태그는 div, h2, h3, p, ul, ol, li, span, strong, em, br, 그리고 간단한 style 속성만 허용하세요.",
        "절대 markdown 코드 블럭(```)이나 backtick 문자를 포함하지 마세요.",
        "잘못된 HTML 태그를 포함하지 마세요. 만들어진 HTML은 안정적으로 렌더링되어야 합니다.",
        "table, thead, tbody, tr, th, td 태그는 절대 사용하지 마세요.",
        "",
        f"총 이슈 수: {summary.get('total', 0)}",
        f"심각도: HIGH={summary['severity'].get('HIGH', 0)}, MEDIUM={summary['severity'].get('MEDIUM', 0)}, LOW={summary['severity'].get('LOW', 0)}",
        f"도구별 이슈 수: {summary.get('tool', {})}",
        "",
        "상위 이슈:",
    ]
    for index, item in enumerate(top_items, start=1):
        lines.append(
            f"{index}. [{item.get('tool')}] {item.get('severity')} {item.get('file')}:{item.get('line')} "
            f"{item.get('rule_id')} - {item.get('message')}"
        )
    lines.extend([
        "",
        "다음 항목을 포함해서 HTML 조각을 작성하세요:",
        "1. 주요 발견 요약", 
        "2. 가장 큰 위험 요소", 
        "3. 개발팀을 위한 권장 조치", 
        "4. 간단한 시각적 요약 (예: div, ul, li, span 기반 막대/배지 표현)",
        "5. 모든 문장은 한국어로 작성하세요.",
    ])
    return "\n".join(lines)


def _build_translation_prompt(messages: list[str]) -> str:
    lines = [
        "아래 보안/정적분석 메시지들을 한국어로 자연스럽고 기술적으로 정확하게 번역하세요.",
        "응답은 반드시 JSON 객체 하나만 반환하세요.",
        "키는 각 메시지의 번호 문자열이고, 값은 한국어 번역 문자열입니다.",
        "원문에 없는 정보는 추가하지 마세요.",
        "rule id, 파일 경로, 코드, API 이름 같은 식별자는 임의로 바꾸지 마세요.",
        "문장이 이미 한국어면 그대로 유지하세요.",
        "",
        "메시지 목록:",
    ]
    for index, message in enumerate(messages, start=1):
        lines.append(f'{index}: {json.dumps(message, ensure_ascii=False)}')
    return "\n".join(lines)


def _issue_signature(item: dict[str, Any]) -> str:
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
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_fix_suggestion_prompt(items: list[dict[str, Any]]) -> str:
    lines = [
        "당신은 보안 코드 리뷰와 정적 분석 결과 해석에 능숙한 시니어 애플리케이션 보안 엔지니어입니다.",
        "아래 이슈 각각에 대해 개발자가 바로 참고할 수 있는 한국어 수정 가이드를 작성하세요.",
        "응답은 반드시 JSON 객체 하나만 반환하세요.",
        "각 키는 제공된 issue_id이고 값은 다음 필드를 가진 JSON 객체여야 합니다:",
        "- title",
        "- why_risky",
        "- recommended_fix",
        "- before_example",
        "- after_example",
        "가능하면 입력 코드 스니펫에 맞는 실제 수정 방향을 제시하세요.",
        "설정 파일 이슈면 설정 맥락에 맞는 예시를, 애플리케이션 코드 이슈면 코드 맥락에 맞는 예시를 제시하세요.",
        "추측은 최소화하되, 안전한 일반 수정 패턴은 구체적으로 제안하세요.",
        "'수동 검토 필요' 같은 일반론으로만 끝내지 마세요.",
        "before_example은 입력 코드에서 핵심 부분을 요약하거나 그대로 활용해도 됩니다.",
        "after_example은 실제로 참고 가능한 예시를 제시하세요.",
        "",
        "이슈 목록:",
    ]
    for item in items:
        lines.extend(
            [
                f"issue_id: {item['issue_id']}",
                f"rule_id: {item.get('rule_id', '')}",
                f"severity: {item.get('severity', '')}",
                f"file: {item.get('file', '')}",
                f"line: {item.get('line', 0)}",
                f"message: {item.get('message', '')}",
                "code:",
                item.get("code", "") or "(코드 정보 없음)",
                "",
            ]
        )
    return "\n".join(lines)


def _clean_html_fragment(html_text: str) -> str:
    text = html_text.strip().replace("\r", "")
    text = re.sub(r"^```(?:html)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    return text


def _looks_like_html(text: str) -> bool:
    return bool(re.search(r"<\s*(div|h[1-6]|p|ul|ol|li|span|strong|em|br|a|style)", text, flags=re.IGNORECASE))


def _sanitize_html(text: str) -> str:
    sanitized = re.sub(r"(?is)<script.*?>.*?</script>", "", text)
    sanitized = re.sub(r"(?i)on\w+=[\"'].*?[\"']", "", sanitized)
    sanitized = re.sub(r"(?is)<table.*?>.*?</table>", "", sanitized)
    sanitized = re.sub(r"(?is)</?(table|thead|tbody|tfoot|tr|th|td)[^>]*>", "", sanitized)
    return sanitized


def _format_inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r"<a href='\2'>\1</a>", text)
    return text


def _render_markdown(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    html_lines: list[str] = []
    open_list = False
    list_type = None
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if open_list:
                html_lines.append(f"</{list_type}>")
                open_list = False
                list_type = None
            continue
        if stripped.startswith("### "):
            if open_list:
                html_lines.append(f"</{list_type}>")
                open_list = False
                list_type = None
            html_lines.append(f"<h3>{_format_inline(stripped[4:])}</h3>")
        elif stripped.startswith("## "):
            if open_list:
                html_lines.append(f"</{list_type}>")
                open_list = False
                list_type = None
            html_lines.append(f"<h2>{_format_inline(stripped[3:])}</h2>")
        elif stripped.startswith("# "):
            if open_list:
                html_lines.append(f"</{list_type}>")
                open_list = False
                list_type = None
            html_lines.append(f"<h1>{_format_inline(stripped[2:])}</h1>")
        elif re.match(r"^[-\*\+]\s+", stripped):
            if not open_list or list_type != "ul":
                if open_list:
                    html_lines.append(f"</{list_type}>")
                open_list = True
                list_type = "ul"
                html_lines.append("<ul>")
            html_lines.append(f"<li>{_format_inline(stripped[2:])}</li>")
        elif re.match(r"^\d+\.\s+", stripped):
            if not open_list or list_type != "ol":
                if open_list:
                    html_lines.append(f"</{list_type}>")
                open_list = True
                list_type = "ol"
                html_lines.append("<ol>")
            html_lines.append(f"<li>{_format_inline(re.sub(r'^\d+\.\s+', '', stripped))}</li>")
        else:
            if open_list:
                html_lines.append(f"</{list_type}>")
                open_list = False
                list_type = None
            html_lines.append(f"<p>{_format_inline(stripped)}</p>")
    if open_list:
        html_lines.append(f"</{list_type}>")
    return "\n".join(html_lines)


def _render_ai_summary(raw: str) -> str:
    cleaned = _clean_html_fragment(raw)
    cleaned = _sanitize_html(cleaned)
    if not cleaned:
        return "<div class='ai-summary-fallback'><p>AI 요약을 생성할 수 없습니다. 응답이 비어 있습니다.</p></div>"
    if _looks_like_html(cleaned):
        return cleaned
    return _render_markdown(cleaned)


def _load_translation_cache(cache_path: Path) -> dict[str, str]:
    if not cache_path.is_file():
        return {}
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_translation_cache(cache_path: Path, cache: dict[str, str]) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _load_json_cache(cache_path: Path) -> dict[str, Any]:
    if not cache_path.is_file():
        return {}
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_json_cache(cache_path: Path, cache: dict[str, Any]) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def translate_issue_messages(messages: list[str], cache_path: Path | None = None) -> dict[str, str]:
    unique_messages = []
    seen: set[str] = set()
    for message in messages:
        normalized = (message or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_messages.append(normalized)

    if not unique_messages:
        return {}

    cache: dict[str, str] = {}
    if cache_path is not None:
        cache = _load_translation_cache(cache_path)

    missing_messages = [message for message in unique_messages if message not in cache]
    if not missing_messages:
        return {message: cache[message] for message in unique_messages}

    if OpenAI is None:
        return {message: cache.get(message, message) for message in unique_messages}

    api_key = _get_openai_key()
    if not api_key:
        return {message: cache.get(message, message) for message in unique_messages}

    prompt = _build_translation_prompt(missing_messages)
    client = OpenAI(api_key=api_key)

    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a precise technical translator. Return only valid JSON."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=1200,
            temperature=0,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content.strip()
        translated = json.loads(content)
        for index, message in enumerate(missing_messages, start=1):
            translated_message = str(translated.get(str(index), "")).strip()
            cache[message] = translated_message or message
        if cache_path is not None:
            _save_translation_cache(cache_path, cache)
    except Exception:
        for message in missing_messages:
            cache.setdefault(message, message)

    return {message: cache.get(message, message) for message in unique_messages}


def generate_fix_suggestions(items: list[dict[str, Any]], cache_path: Path | None = None) -> dict[str, dict[str, str]]:
    normalized_items = []
    seen_ids: set[str] = set()
    for item in items:
        issue_id = _issue_signature(item)
        if issue_id in seen_ids:
            continue
        seen_ids.add(issue_id)
        normalized_items.append(
            {
                "issue_id": issue_id,
                "rule_id": item.get("rule_id", ""),
                "severity": item.get("severity", ""),
                "file": item.get("file", ""),
                "line": item.get("line", 0),
                "message": item.get("message", ""),
                "code": item.get("code", ""),
            }
        )

    if not normalized_items:
        return {}

    cache: dict[str, Any] = {}
    if cache_path is not None:
        cache = _load_json_cache(cache_path)

    missing_items = [item for item in normalized_items if item["issue_id"] not in cache]
    if missing_items and OpenAI is not None and _get_openai_key():
        prompt = _build_fix_suggestion_prompt(missing_items)
        client = OpenAI(api_key=_get_openai_key())
        try:
            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are a precise application security remediation assistant. Return only valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=2400,
                temperature=0.1,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content.strip()
            parsed = json.loads(content)
            for item in missing_items:
                value = parsed.get(item["issue_id"], {})
                cache[item["issue_id"]] = {
                    "title": str(value.get("title", "")).strip(),
                    "why_risky": str(value.get("why_risky", "")).strip(),
                    "recommended_fix": str(value.get("recommended_fix", "")).strip(),
                    "before_example": str(value.get("before_example", "")).strip(),
                    "after_example": str(value.get("after_example", "")).strip(),
                }
            if cache_path is not None:
                _save_json_cache(cache_path, cache)
        except Exception:
            pass

    results: dict[str, dict[str, str]] = {}
    for item in normalized_items:
        cached = cache.get(item["issue_id"], {})
        if isinstance(cached, dict):
            results[item["issue_id"]] = {
                "title": str(cached.get("title", "")).strip(),
                "why_risky": str(cached.get("why_risky", "")).strip(),
                "recommended_fix": str(cached.get("recommended_fix", "")).strip(),
                "before_example": str(cached.get("before_example", "")).strip(),
                "after_example": str(cached.get("after_example", "")).strip(),
            }
    return results


def generate_ai_summary(items: list[dict], summary: dict) -> str:
    if OpenAI is None:
        return "<div class='ai-summary-fallback'><p>AI 요약을 생성할 수 없습니다: openai 패키지가 설치되지 않았습니다.</p></div>"

    api_key = _get_openai_key()
    if not api_key:
        return "<div class='ai-summary-fallback'><p>AI 요약을 생성할 수 없습니다: .env에 OPENAI_API_KEY가 설정되지 않았습니다.</p></div>"

    prompt = _build_prompt(items, summary)
    client = OpenAI(api_key=api_key)

    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a helpful software security report assistant."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=400,
            temperature=0.7,
        )
        content = response.choices[0].message.content.strip()
        return _render_ai_summary(content)
    except Exception as error:
        return f"<div class='ai-summary-fallback'><p>AI 요약을 생성할 수 없습니다: {html.escape(str(error))}</p></div>"
