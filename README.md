# Dev Analyzer Inspector

간단한 MVP 도구로 Git 리포지토리를 클론하고 정적 분석 결과를 통합 리포트로 생성합니다.

## 기능

- Git 리포지토리 클론
- 프로젝트 유형 감지 (Python / JS)
- Semgrep, ESLint, Bandit 실행
- JSON 통합 결과 생성 (`output/merged_report.json`)
- HTML 리포트 생성 (`output/report.html`)

## 요구 사항

- Python 3.9+
- `git` CLI 설치
- 시스템에 `semgrep`, `eslint`, `bandit` 설치
- Python 패키지: `Jinja2`

## 설치

```bash
pip install -r requirements.txt
```

## 환경 설정

프로젝트에서 `.env.example` 를 복사하여 `.env` 파일을 만들고 필요에 따라 값을 수정할 수 있습니다.

```bash
copy .env.example .env
```

현재는 기본 브랜치, 작업 디렉터리 설정과 OpenAI API 키를 위한 값이 포함됩니다.

## 실행

운영체제와 셸 환경에 따라 실행 파일이 다릅니다.

### Windows PowerShell

Windows에서는 `run.ps1` 사용을 권장합니다.

```powershell
.\run.ps1 https://github.com/example/repo.git
```

브랜치를 지정하려면:

```powershell
.\run.ps1 https://github.com/example/repo.git main
```

### Linux / macOS / WSL / Git Bash

bash 환경에서는 `run.sh`를 사용할 수 있습니다.

```bash
./run.sh https://github.com/example/repo.git
```

브랜치를 지정하려면:

```bash
./run.sh https://github.com/example/repo.git main
```

### 직접 Python으로 실행

스크립트를 사용하지 않고 직접 실행할 수도 있습니다.

```bash
python app/main.py https://github.com/example/repo.git --branch main
```

## 출력

- `output/semgrep.json`
- `output/eslint.json` (JS 프로젝트일 경우, ESLint 설정이 없으면 건너뜁니다)
- `output/bandit.json` (Python 프로젝트일 경우)
- `output/merged_report.json`
- `output/report.html`

## AI 요약

`.env`에 `OPENAI_API_KEY`를 추가하면 AI가 리포트 결과를 요약하여 `output/report.html`에 포함합니다. 키가 없으면 AI 요약은 생략됩니다.

## 규칙 관리

리포트의 심각도 보정과 수정 제안은 `templates/issue_rules.json` 파일에서 함께 관리합니다.

각 규칙은 다음 역할을 동시에 가질 수 있습니다.

- 심각도 보정 (`severity`)
- 수정 제안 템플릿 (`fix_suggestion`)
- 매칭 조건 정의 (`match_any`, `match_all`, `file_match_any` 등)

### 주요 필드

- `id`: 규칙 식별자
- `priority`: 숫자가 작을수록 먼저 매칭
- `tool`: 특정 도구에만 적용할 때 사용 (`semgrep`, `eslint`, `bandit`)
- `match_any`: `rule_id` 또는 메시지에 하나라도 포함되면 매칭
- `match_all`: 모두 포함되어야 매칭
- `exclude_any`: 포함되면 매칭 제외
- `message_match_any`: 원문 메시지 기준 부분 매칭
- `message_match_all`: 원문 메시지 기준 전체 조건 매칭
- `file_match_any`: 파일 경로 glob 매칭
- `severity`: `HIGH`, `MEDIUM`, `LOW`
- `fix_suggestion`: 리포트에 표시할 수정 제안 템플릿

### `fix_suggestion` 필드

- `title`
- `why_risky`
- `recommended_fix`
- `before_example`
- `after_example`

### 사용할 수 있는 플레이스홀더

- `{code_context}`
- `{message}`
- `{raw_message}`
- `{file}`
- `{line}`
- `{rule_id}`
- `{severity}`
- `{tool}`

### 규칙 추가 예시

```json
{
  "id": "example_rule",
  "priority": 100,
  "tool": "semgrep",
  "match_any": ["example-rule-id", "dangerous pattern"],
  "file_match_any": ["*.ts", "*.tsx"],
  "severity": "HIGH",
  "fix_suggestion": {
    "title": "위험한 패턴을 안전한 방식으로 변경",
    "why_risky": "{tool}가 감지한 이 패턴은 권한 상승 또는 정보 노출로 이어질 수 있습니다.",
    "recommended_fix": "문제 패턴을 제거하고 검증된 안전 API로 대체하세요.",
    "before_example": "{code_context}",
    "after_example": "// 안전한 대체 코드 예시"
  }
}
```

### 운영 원칙

- 구체적인 규칙은 `priority`를 더 낮게 두고, 범용 fallback 규칙은 뒤에 배치하세요.
- 심각도만 보정하고 싶으면 `severity`만 넣어도 됩니다.
- 수정 제안만 추가하고 싶으면 `fix_suggestion`만 넣어도 됩니다.
- 새 규칙을 추가한 뒤 리포트를 다시 생성하면 즉시 반영됩니다.

## 주의

- ESLint는 리포지토리에 `eslint.config.js` 또는 `.eslintrc.*` 설정이 있으면 그 설정을 사용합니다.
- 설정이 없을 경우, 이 도구는 기본 ESLint flat config 파일 `templates/eslint.default.config.cjs`를 주입하여 분석을 시도합니다.
- `eslint` 또는 `npx`가 없으면 해당 도구 결과는 건너뛰고 나머지 분석은 계속 진행됩니다.
