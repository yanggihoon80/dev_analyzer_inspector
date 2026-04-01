# dev_analyzer_inspector

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

```bash
python .\app\main.py https://github.com/example/repo.git
```

또는 GitHub 리포지토리와 브랜치를 함께 지정:

```bash
python .\app\main.py https://github.com/example/repo.git --branch main
```

`run.sh`를 사용할 경우:

```bash
bash .\run.sh https://github.com/example/repo.git
```

## 출력

- `output/semgrep.json`
- `output/eslint.json` (JS 프로젝트일 경우, ESLint 설정이 없으면 건너뜁니다)
- `output/bandit.json` (Python 프로젝트일 경우)
- `output/merged_report.json`
- `output/report.html`

## AI 요약

`.env`에 `OPENAI_API_KEY`를 추가하면 AI가 리포트 결과를 요약하여 `output/report.html`에 포함합니다. 키가 없으면 AI 요약은 생략됩니다.

## 주의

- ESLint는 리포지토리에 `eslint.config.js` 또는 `.eslintrc.*` 설정이 있으면 그 설정을 사용합니다.
- 설정이 없을 경우, 이 도구는 기본 ESLint flat config 파일 `templates/eslint.default.config.cjs`를 주입하여 분석을 시도합니다.
- `eslint` 또는 `npx`가 없으면 해당 도구 결과는 건너뛰고 나머지 분석은 계속 진행됩니다.
