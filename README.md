# Dev Analyzer Inspector

간단한 MVP 도구로 Git 리포지토리를 클론하고 정적 분석 결과를 통합 리포트로 생성합니다.

## 기능

- Git 리포지토리 클론
- `workspace/`에 동일한 이름의 리포지토리가 있으면 재사용, 없으면 클론
- 프로젝트 유형 감지 (Python / JS)
- Semgrep, ESLint, Bandit 실행
- 선택적으로 Newman 기반 API 테스트 실행
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

현재는 저장소 URL, 기본 브랜치, 작업 디렉터리 설정과 OpenAI API 키를 위한 값이 포함됩니다.
`REPO_URL`, `GIT_BRANCH`, `WORKSPACE_DIR`, `OUTPUT_DIR` 는 실행 시 기본값으로 사용되며, CLI 인자를 넘기면 CLI 값이 우선합니다.
분석 종류는 `ANALYSIS_TARGETS` 로 제어할 수 있으며 기본값은 `static,api` 입니다.
`AI_REPORT_ENABLED` 는 AI 요약, 메시지 번역, 수정 제안 생성 사용 여부를 제어합니다.

예시:

- `ANALYSIS_TARGETS=static,api`: 정적 코드 분석 + API 테스트
- `ANALYSIS_TARGETS=static`: 정적 코드 분석만 실행
- `ANALYSIS_TARGETS=api`: API 테스트만 실행
- `AI_REPORT_ENABLED=false`: OpenAI 기반 요약/번역/수정 제안 생성 비활성화

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

`.env`에 `REPO_URL`이 설정되어 있으면 URL 인자 없이도 실행할 수 있습니다.

```bash
python app/main.py
```

## 출력

- `output/semgrep.json`
- `output/eslint.json` (JS 프로젝트일 경우, ESLint 설정이 없으면 건너뜁니다)
- `output/bandit.json` (Python 프로젝트일 경우)
- `output/api_test.json` (API 테스트 설정이 있는 경우)
- `output/merged_report.json`
- `output/report.html`

## API 테스트 통합

분석 대상 리포지토리 루트에 `.dev-analyzer.yml` 파일을 추가하면 정적 분석과 함께 API 테스트 결과를 리포트에 포함할 수 있습니다.
API 테스트가 활성화되어 있고 설정 파일이 없으면, 이 도구는 프로젝트 루트의 `.dev-analyzer.example.yml`을 대상 리포지토리 루트의 `.dev-analyzer.yml`로 자동 복사합니다.

```yaml
api_test:
  enabled: true
  runner: newman
  start_command: "npm run dev"
  start_cwd: "."
  base_url: "http://127.0.0.1:3000"
  healthcheck:
    path: "/health"
    timeout_seconds: 60
    interval_seconds: 2
  env:
    NODE_ENV: "test"
  newman:
    collection: "tests/postman/collection.json"
    environment: "tests/postman/environment.json"
    reporters: ["json"]
```

### `.dev-analyzer.yml` 항목 설명

- `api_test.enabled`
  - API 테스트 사용 여부입니다.
  - `true`, `false`

- `api_test.runner`
  - 현재 지원하는 API 테스트 실행기입니다.
  - 현재는 `newman`만 지원합니다.

- `api_test.start_command`
  - API 서버를 실행할 명령입니다.
  - 예: `npm run dev`, `pnpm run dev`

- `api_test.start_cwd`
  - `start_command`를 실행할 작업 디렉터리입니다.
  - 예: `.`, `apps/server`

- `api_test.base_url`
  - API 서버 기본 주소입니다.
  - 예: `http://127.0.0.1:4000`

- `api_test.runtime.node_env`
  - API 서버 실행 시 주입할 `NODE_ENV` 값입니다.
  - 예: `test`, `development`, `production`

- `api_test.runtime.port`
  - API 서버 실행 시 주입할 `PORT` 값입니다.
  - 예: `3000`, `4000`

- `api_test.database.type`
  - 테스트용 데이터베이스 종류입니다.
  - 현재 내부 처리 기준으로 `postgresql`, `postgres`, `mysql`, `mariadb` 값을 기대합니다.

- `api_test.database.url`
  - 직접 사용할 DB 연결 문자열입니다.
  - 값이 있으면 `host`, `port`, `name`, `user`, `password`보다 우선합니다.

- `api_test.database.host`
- `api_test.database.port`
- `api_test.database.name`
- `api_test.database.user`
- `api_test.database.password`
  - `database.url`을 직접 쓰지 않을 때 DB 연결 문자열을 조합하기 위한 값입니다.

- `api_test.database.init.enabled`
  - 테스트 시작 전에 테스트 DB 초기화를 수행할지 여부입니다.
  - `true`, `false`

- `api_test.database.init.mode`
  - 테스트 DB 초기화 방식입니다.
  - 현재는 `db_push`를 사용합니다.

- `api_test.database.init.seed`
  - 과거 호환용 항목입니다.
  - 현재는 `.dev-analyzer.seed.json` 또는 `.dev-analyzer.seed/` 디렉터리의 외부 seed 파일을 자동으로 읽어, DB에 데이터가 없을 때만 주입하는 방식을 우선 사용합니다.

- `api_test.redis.host`
- `api_test.redis.port`
  - API 서버 실행 시 주입할 Redis 연결 정보입니다.

- `api_test.docker.services`
  - 테스트 중 자동으로 띄울 Docker Compose 서비스 목록입니다.
  - 예: `["db", "redis"]`

- `api_test.docker.cleanup`
  - API 테스트가 끝난 뒤 Docker Compose 서비스를 어떻게 정리할지 결정합니다.
  - 허용값:
    - `keep`: 아무 것도 하지 않습니다. 컨테이너를 계속 실행 상태로 둡니다.
    - `stop`: 지정한 서비스를 정지만 합니다. 컨테이너는 남아 있습니다.
    - `down`: `docker compose down`을 실행합니다.
    - `down_volumes`: `docker compose down -v`를 실행합니다. 볼륨까지 제거합니다.

- `api_test.healthcheck.path`
  - 서버 준비 완료를 확인할 health check 경로입니다.
  - 예: `/health`

- `api_test.healthcheck.timeout_seconds`
  - health check 최대 대기 시간(초)입니다.

- `api_test.healthcheck.interval_seconds`
  - health check 재시도 간격(초)입니다.

- `api_test.env`
  - 서버 실행 시 추가로 주입할 환경변수입니다.
  - 예:
    ```yaml
    env:
      NODE_ENV: "test"
      PORT: "4000"
    ```

- `api_test.newman.collection`
  - 실행할 Postman/Newman 컬렉션 파일 경로입니다.

- `api_test.newman.environment`
  - 선택 항목입니다.
  - Newman environment 파일 경로입니다.

- `api_test.newman.reporters`
  - Newman reporter 목록입니다.
  - 예: `["json"]`

### 동작 방식

- 분석기가 대상 프로젝트 서버를 `start_command`로 실행합니다.
- 서버 실행에 필요한 의존성이 없어 보이면 lockfile 기준으로 `pnpm install`, `npm install`, `yarn install` 중 하나를 자동 시도합니다.
- 테스트 DB/Redis를 Docker로 띄우는 프로젝트는 Docker Desktop이 설치되어 있어야 합니다.
- Windows에서는 Docker Engine이 꺼져 있으면 Docker Desktop 실행을 자동 시도합니다.
- 테스트 DB 스키마를 적용한 뒤, 분석 대상 프로젝트 루트의 외부 seed 파일을 확인합니다.
- 외부 seed 파일이 있으면 대상 모델의 현재 row 수를 조회합니다.
- 해당 모델에 데이터가 이미 있으면 seed를 건너뜁니다.
- 해당 모델이 비어 있으면 외부 JSON/CSV 데이터를 DB에 주입합니다.
- `healthcheck.path`가 응답할 때까지 대기합니다.
- Newman 컬렉션을 실행하고 JSON 결과를 저장합니다.
- 실패한 API 테스트를 정적 분석 결과와 함께 `merged_report.json` 및 `report.html`에 포함합니다.

### 요구 사항

- 대상 프로젝트에서 API 서버를 실행할 수 있어야 합니다.
- 시스템에 `newman` 또는 `npx`가 설치되어 있어야 합니다.
- YAML 설정 파일을 읽기 위해 이 프로젝트에는 `PyYAML` 의존성이 필요합니다.
- 테스트 DB/Redis를 Docker Compose로 올리는 프로젝트라면 Docker Desktop이 설치되어 있고 실행 가능한 상태여야 합니다.

### 외부 Seed 데이터

옵션을 추가로 주지 않아도, 분석 대상 프로젝트 루트에 아래 경로 중 하나가 있으면 자동으로 외부 seed 데이터를 읽습니다.

- `.dev-analyzer.seed.json`
- `.dev-analyzer.seed/*.json`
- `.dev-analyzer.seed/*.csv`

외부 seed 파일이 전혀 없으면, 이 도구는 아래 우선순위로 프로젝트 구조를 읽어서 `.dev-analyzer.seed.json` 초안을 자동 생성합니다.

1. `prisma/schema.prisma`
2. `schema.sql`, `migrations/*.sql` 같은 SQL 파일
3. `*.entity.ts`, `*.controller.ts`, `*.service.ts` 같은 소스코드

동작 규칙은 다음과 같습니다.

- 외부 seed 파일이 없으면 `.dev-analyzer.seed.json` 초안을 자동 생성합니다.
- 스키마 적용 후 seed 파일을 확인합니다.
- seed 파일에 지정된 모델에 대해 현재 DB row 수를 조회합니다.
- row가 이미 있으면 해당 모델 seed는 건너뜁니다.
- row가 0건이면 seed 데이터를 주입합니다.
- 따라서 `cleanup: keep`일 때도 기존 데이터가 있으면 중복 삽입하지 않습니다.

#### JSON 예시

```json
{
  "industry": [
    { "name": "IT/소프트웨어", "code": "IT", "description": "IT 및 소프트웨어 산업" }
  ],
  "agreement": [
    { "type": "TERMS", "version": 1 }
  ]
}
```

- JSON의 key는 Prisma model accessor 이름 기준입니다.
  - 예: `industry`, `agreement`, `memberRole`
- value는 삽입할 row 배열입니다.

#### 디렉터리 기반 예시

- `.dev-analyzer.seed/industry.json`
- `.dev-analyzer.seed/agreement.csv`

파일명에서 model 이름을 추론합니다.
숫자 prefix를 붙여 순서를 제어해도 됩니다.

- `01_industry.json`
- `02_agreement.csv`

#### CSV 규칙

- 첫 줄은 header입니다.
- 각 row는 DB에 넣을 데이터 한 건입니다.
- 빈 문자열은 `null`로 처리합니다.
- `true`, `false`, 숫자 형태 값은 기본형으로 자동 변환합니다.

예시 파일은 [`.dev-analyzer.seed.example.json`](D:/dev/linkvalue_dev_analyzer_agent/.dev-analyzer.seed.example.json) 에 있습니다.

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


## Auto Test Design

The API test engine can now generate test design artifacts automatically at runtime.

- Generated files are created inside the analyzed project:
  - `.dev-analyzer.seed.json`
  - `.dev-analyzer.auth-matrix.json`
  - `.dev-analyzer-tools/generated/auto-test-blueprint.json`
  - `.dev-analyzer-tools/generated/auto-smoke.collection.json`
- These generated files are also added to `.git/info/exclude` automatically so they do not pollute normal Git status.

### LLM toggle

Add these keys to `.env` to control automatic design generation:

```env
API_TEST_AUTO_LLM_ENABLED=true
API_TEST_AUTO_MODEL=gpt-4o-mini
API_TEST_WRITE_SUCCESS_ENABLED=false
```

- `API_TEST_AUTO_LLM_ENABLED=true`
  - Use LLM inference first when building the initial API test design.
  - If LLM inference fails or is unavailable, the engine falls back to built-in heuristics.
- `API_TEST_AUTO_LLM_ENABLED=false`
  - Skip LLM inference entirely and always use the heuristic fallback.
- `API_TEST_AUTO_MODEL`
  - Select the model used for design inference.
- `API_TEST_WRITE_SUCCESS_ENABLED=false`
  - Keep automatic write-method tests in safe negative mode by default.
  - When `false`, generated `POST/PUT/PATCH/DELETE` tests focus on unauthorized, forbidden, and validation-style failures.
  - When `true`, the engine may also generate authorized write-success cases if it can infer a sample payload from existing Postman collections.

### Reuse behavior

- The engine does not regenerate the design on every run.
- If these files already exist, the engine reuses them:
  - `.dev-analyzer-tools/generated/auto-test-blueprint.json`
  - `.dev-analyzer-tools/generated/auto-smoke.collection.json`
- If they do not exist, the engine creates them automatically before the API test starts.

### Force refresh

Use this argument to regenerate the design files intentionally:

```bash
python app/main.py --refresh-test-design
```

This forces the engine to rebuild the blueprint and collection even if existing generated files are already present.
