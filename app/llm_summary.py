import html
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
        "HTML 태그는 div, h2, h3, p, ul, ol, li, table, tr, th, td, span, strong, em, br, 그리고 간단한 style 속성만 허용하세요.",
        "절대 markdown 코드 블럭(```)이나 backtick 문자를 포함하지 마세요.",
        "잘못된 HTML 태그를 포함하지 마세요. 만들어진 HTML은 안정적으로 렌더링되어야 합니다.",
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
        "4. 간단한 표 또는 시각적 요약 (예: 막대, 표)",
        "5. 모든 문장은 한국어로 작성하세요.",
    ])
    return "\n".join(lines)


def _clean_html_fragment(html_text: str) -> str:
    text = html_text.strip().replace("\r", "")
    text = re.sub(r"^```(?:html)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    return text


def _looks_like_html(text: str) -> bool:
    return bool(re.search(r"<\s*(div|h[1-6]|p|ul|ol|li|table|tr|th|td|span|strong|em|br|a|style|tbody|thead|tfoot)", text, flags=re.IGNORECASE))


def _sanitize_html(text: str) -> str:
    sanitized = re.sub(r"(?is)<script.*?>.*?</script>", "", text)
    sanitized = re.sub(r"(?i)on\w+=[\"'].*?[\"']", "", sanitized)
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
