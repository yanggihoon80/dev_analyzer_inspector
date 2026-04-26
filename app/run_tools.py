import csv
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO_CONFIG_TEMPLATE = PROJECT_ROOT / ".dev-analyzer.example.yml"
AUTO_TEST_BLUEPRINT_VERSION = 5


def _resolve_command(program: str, module: str | None = None) -> list[str]:
    if shutil.which(program):
        return [program]
    if module:
        return [sys.executable, "-m", module]
    raise FileNotFoundError(f"명령어를 찾을 수 없습니다: {program}")


def _build_process_env(extra_env: dict | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if isinstance(extra_env, dict):
        for key, value in extra_env.items():
            if value is not None:
                env[str(key)] = str(value)
    return env


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _build_database_url_from_env() -> str:
    explicit_url = os.getenv("API_TEST_DATABASE_URL", "").strip()
    if explicit_url:
        return explicit_url

    host = os.getenv("API_TEST_DB_HOST", "").strip()
    port = os.getenv("API_TEST_DB_PORT", "").strip()
    name = os.getenv("API_TEST_DB_NAME", "").strip()
    user = os.getenv("API_TEST_DB_USER", "").strip()
    password = os.getenv("API_TEST_DB_PASSWORD", "").strip()
    if all([host, port, name, user, password]):
        return f"postgresql://{user}:{password}@{host}:{port}/{name}"
    return ""


def _get_api_test_env_defaults() -> dict[str, str]:
    defaults: dict[str, str] = {}

    node_env = os.getenv("API_TEST_NODE_ENV", "").strip()
    if node_env:
        defaults["NODE_ENV"] = node_env

    port = os.getenv("API_TEST_PORT", "").strip()
    if port:
        defaults["PORT"] = port

    database_url = _build_database_url_from_env()
    if database_url:
        defaults["DATABASE_URL"] = database_url

    redis_host = os.getenv("API_TEST_REDIS_HOST", "").strip()
    if redis_host:
        defaults["REDIS_HOST"] = redis_host

    redis_port = os.getenv("API_TEST_REDIS_PORT", "").strip()
    if redis_port:
        defaults["REDIS_PORT"] = redis_port

    return defaults


def _build_database_url_from_config(database_config: dict) -> str:
    if not isinstance(database_config, dict):
        return ""

    explicit_url = str(database_config.get("url") or "").strip()
    if explicit_url:
        return explicit_url

    database_type = str(database_config.get("type") or "postgresql").strip().lower()
    host = str(database_config.get("host") or "").strip()
    port = str(database_config.get("port") or "").strip()
    name = str(database_config.get("name") or "").strip()
    user = str(database_config.get("user") or "").strip()
    password = str(database_config.get("password") or "").strip()
    if all([host, port, name, user, password]):
        if database_type in {"postgres", "postgresql"}:
            scheme = "postgresql"
        elif database_type in {"mysql", "mariadb"}:
            scheme = database_type
        else:
            scheme = database_type or "postgresql"
        return f"{scheme}://{user}:{password}@{host}:{port}/{name}"
    return ""


def _load_env_file_values(repo_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    candidates = [
        repo_path / ".env",
        repo_path / "apps" / "server" / ".env",
        repo_path / "setup" / "server" / ".env",
    ]

    for path in candidates:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, raw_value = stripped.split("=", 1)
            key = key.strip()
            value = raw_value.strip().strip('"').strip("'")
            if not key or not value:
                continue
            values[key] = value

    return values


def _build_api_test_runtime_env(api_test: dict) -> dict[str, str]:
    runtime_env = _get_api_test_env_defaults()

    repo_path_value = api_test.get("__repo_path")
    if repo_path_value:
        runtime_env.update(_load_env_file_values(Path(str(repo_path_value))))

    explicit_env = api_test.get("env") if isinstance(api_test.get("env"), dict) else {}
    for key, value in explicit_env.items():
        if value is not None:
            runtime_env[str(key)] = str(value)

    runtime_config = api_test.get("runtime") if isinstance(api_test.get("runtime"), dict) else {}
    node_env = str(runtime_config.get("node_env") or "").strip()
    if node_env:
        runtime_env["NODE_ENV"] = node_env

    port = str(runtime_config.get("port") or "").strip()
    if port:
        runtime_env["PORT"] = port

    database_url = _build_database_url_from_config(api_test.get("database") or {})
    if database_url:
        runtime_env["DATABASE_URL"] = database_url

    redis_config = api_test.get("redis") if isinstance(api_test.get("redis"), dict) else {}
    redis_host = str(redis_config.get("host") or "").strip()
    if redis_host:
        runtime_env["REDIS_HOST"] = redis_host

    redis_port = str(redis_config.get("port") or "").strip()
    if redis_port:
        runtime_env["REDIS_PORT"] = redis_port

    # Bearer-token flows need a signing secret even when the target repo
    # does not ship a filled local .env. Use a deterministic test default
    # only when the project did not provide one.
    runtime_env.setdefault("JWT_SECRET", "dev-analyzer-test-secret")
    runtime_env.setdefault("JWT_ACCESS_EXPIRATION", "1h")
    runtime_env.setdefault("JWT_REFRESH_EXPIRATION", "7d")

    return runtime_env


def _get_api_test_docker_services(api_test: dict) -> list[str]:
    docker_config = api_test.get("docker") if isinstance(api_test.get("docker"), dict) else {}
    services = docker_config.get("services")
    if isinstance(services, list):
        normalized = [str(service).strip() for service in services if str(service).strip()]
        if normalized:
            return normalized

    legacy_services = api_test.get("docker_services")
    if isinstance(legacy_services, list):
        normalized = [str(service).strip() for service in legacy_services if str(service).strip()]
        if normalized:
            return normalized

    raw_services = os.getenv("API_TEST_DOCKER_SERVICES", "db,redis")
    return [service.strip() for service in raw_services.split(",") if service.strip()]


def _get_api_test_docker_cleanup_mode(api_test: dict) -> str:
    docker_config = api_test.get("docker") if isinstance(api_test.get("docker"), dict) else {}
    cleanup = str(docker_config.get("cleanup") or "keep").strip().lower()
    valid = {"keep", "stop", "down", "down_volumes"}
    return cleanup if cleanup in valid else "keep"


def _get_database_init_config(api_test: dict) -> dict:
    database_config = api_test.get("database") if isinstance(api_test.get("database"), dict) else {}
    init_config = database_config.get("init") if isinstance(database_config.get("init"), dict) else {}
    return {
        "enabled": bool(init_config.get("enabled", True)),
        "mode": str(init_config.get("mode") or "db_push").strip().lower(),
        "seed": bool(init_config.get("seed", False)),
    }


def _run_command(command: list[str], cwd: Path, output_path: Path) -> Path:
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_build_process_env(),
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "Tool execution failed"
        raise RuntimeError(message)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(completed.stdout or "{}", encoding="utf-8")
    return output_path


def _format_env_file_value(value: str) -> str:
    text = str(value)
    if any(char in text for char in [" ", "#", "\n", "\r", '"']):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _prepare_runtime_env_file(start_cwd: Path, extra_env: dict[str, str]) -> tuple[Path | None, str | None]:
    if not extra_env:
        return None, None

    env_path = start_cwd / ".env"
    original_content = env_path.read_text(encoding="utf-8") if env_path.exists() else None

    lines: list[str] = []
    remaining_env = {str(key).strip(): str(value) for key, value in extra_env.items() if str(key).strip()}
    if original_content:
        for line in original_content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                lines.append(line)
                continue
            key = stripped.split("=", 1)[0].strip()
            if key in remaining_env:
                lines.append(f"{key}={_format_env_file_value(remaining_env.pop(key))}")
            else:
                lines.append(line)

    if lines and lines[-1].strip():
        lines.append("")
    lines.append("# Added by Dev Analyzer Inspector for API tests")

    for key, value in remaining_env.items():
        lines.append(f"{key}={_format_env_file_value(value)}")

    env_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return env_path, original_content


def _normalize_collection_request_url(request_url: object) -> str:
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


def _infer_authorization_role_label(test_name: str, request: dict | None = None) -> str:
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

    combined = " ".join([name, auth_value.lower()]).strip()
    if "adminaccesstoken" in combined or "admin bearer token" in combined:
        return "Admin"
    if "lawyeraccesstoken" in combined or "lawyer bearer token" in combined:
        return "Lawyer"
    if "companyaccesstoken" in combined or "company bearer token" in combined:
        return "Company Manager"
    return "Public"


def _infer_authorization_expectation(test_name: str, request: dict | None = None) -> str | None:
    name = str(test_name or "").lower()
    role = _infer_authorization_role_label(test_name, request)
    if any(token in name for token in ["returns forbidden", "returns unauthorized", "returns inaccessible"]):
        return "deny"
    if role != "Public" and "bearer token" in name:
        return "allow"
    if role == "Public" and any(
        token in name
        for token in [
            "returns ok",
            "is reachable",
            "returns page response",
            "returns array",
            "returns nickname",
            "returns availability",
        ]
    ):
        return "allow"
    return None


def _build_authorization_matrix(collection_path: Path) -> dict:
    if not collection_path.is_file():
        return {"collection": collection_path.name, "routes": {}}

    try:
        payload = json.loads(collection_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"collection": collection_path.name, "routes": {}}

    routes: dict[str, dict] = {}
    for item in payload.get("item", []):
        if not isinstance(item, dict):
            continue
        request = item.get("request") if isinstance(item.get("request"), dict) else {}
        method = str(request.get("method") or "").strip().upper()
        endpoint = _normalize_collection_request_url(request.get("url"))
        test_name = str(item.get("name") or "").strip()
        if not method or not endpoint or not test_name:
            continue

        role = _infer_authorization_role_label(test_name, request)
        expectation = _infer_authorization_expectation(test_name, request)
        if expectation is None:
            continue

        route_key = f"{method} {endpoint}"
        route = routes.setdefault(
            route_key,
            {
                "method": method,
                "endpoint": endpoint,
                "roles": {},
            },
        )
        role_entry = route["roles"].setdefault(
            role,
            {
                "expectations": [],
                "source_tests": [],
            },
        )
        if expectation not in role_entry["expectations"]:
            role_entry["expectations"].append(expectation)
        role_entry["source_tests"].append(test_name)

    return {
        "collection": collection_path.name,
        "routes": routes,
    }


def _write_authorization_matrix(repo_path: Path, collection_path: Path) -> tuple[Path, dict]:
    matrix = _build_authorization_matrix(collection_path)
    matrix_path = repo_path / ".dev-analyzer.auth-matrix.json"
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"    · 권한 기대 매트릭스 자동 생성: {matrix_path}")
    return matrix_path, matrix


def _get_auto_test_model() -> str:
    return str(
        os.getenv("API_TEST_AUTO_MODEL")
        or os.getenv("OPENAI_API_MODEL")
        or "gpt-4o-mini"
    ).strip()


def _is_auto_test_llm_enabled() -> bool:
    raw = str(os.getenv("API_TEST_AUTO_LLM_ENABLED") or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _get_openai_api_key() -> str:
    return str(os.getenv("OPENAI_API_KEY") or "").strip()


def _is_auto_test_write_success_enabled() -> bool:
    raw = str(os.getenv("API_TEST_WRITE_SUCCESS_ENABLED") or "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _ensure_generated_files_excluded_from_git(repo_path: Path) -> None:
    exclude_path = repo_path / ".git" / "info" / "exclude"
    if not exclude_path.parent.is_dir():
        return

    patterns = [
        ".dev-analyzer.seed.json",
        ".dev-analyzer.auth-matrix.json",
        ".dev-analyzer-tools/",
    ]
    try:
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.is_file() else ""
    except OSError:
        return

    lines = existing.splitlines()
    changed = False
    for pattern in patterns:
        if pattern in lines:
            continue
        lines.append(pattern)
        changed = True

    if not changed:
        return

    content = "\n".join(line for line in lines if line.strip()) + "\n"
    try:
        exclude_path.write_text(content, encoding="utf-8")
    except OSError:
        return


def _join_route_path(base_path: str, method_path: str) -> str:
    base = "/" + "/".join(part for part in str(base_path or "").strip("/").split("/") if part)
    tail = "/".join(part for part in str(method_path or "").strip("/").split("/") if part)
    if not tail:
        return base or "/"
    return f"{base.rstrip('/')}/{tail}".replace("//", "/")


def _discover_project_api_endpoints(repo_path: Path) -> list[dict]:
    controller_pattern = re.compile(r"@Controller\(\s*['\"]([^'\"]*)['\"]\s*\)")
    method_pattern = re.compile(r"@(Get|Post|Put|Patch|Delete|Options|Head)\(\s*(?:['\"]([^'\"]*)['\"])?\s*\)")
    skip_parts = {"node_modules", ".git", "dist", "build", "output", ".next"}
    discovered: dict[tuple[str, str], dict] = {}

    for controller_file in sorted(repo_path.rglob("*.controller.ts")):
        if any(part in skip_parts for part in controller_file.parts):
            continue
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


def _is_dynamic_endpoint(endpoint: str) -> bool:
    return ":" in endpoint or "{" in endpoint or "}" in endpoint


def _is_callback_or_integration_endpoint(endpoint: str) -> bool:
    lowered = endpoint.lower()
    return any(
        token in lowered
        for token in [
            "callback",
            "webhook",
            "oauth",
            "identity",
            "interactions",
            "result",
            "download",
            "upload",
            "presigned",
        ]
    )


def _materialize_endpoint_for_smoke(endpoint: str) -> str:
    replacements = {
        "memberCode": "LAW001",
        "companyId": "1",
        "jdId": "1",
        "postId": "1",
        "resumeId": "1",
        "specUpId": "1",
        "verificationId": "1",
        "fileId": "1",
        "commentId": "1",
        "reportId": "1",
        "id": "1",
    }

    def replace_token(match: re.Match[str]) -> str:
        token = str(match.group(1) or "")
        return replacements.get(token, "1")

    return re.sub(r":([A-Za-z0-9_]+)", replace_token, endpoint)


def _looks_public_endpoint(endpoint: str) -> bool:
    lowered = endpoint.lower()
    if lowered in {"/", "/health", "/docs", "/swagger", "/openapi.json"}:
        return True
    if lowered.startswith("/admin"):
        return False
    if any(token in lowered for token in ["/popular", "/insights", "/nickname/random"]):
        return True
    if lowered == "/api/v1/lawyer/job-descriptions":
        return True
    if any(token in lowered for token in ["/me", "/my", "/mine"]):
        return False
    if any(token in lowered for token in ["login", "logout", "reissue", "refresh", "reset", "verification"]):
        return False
    if any(
        token in lowered
        for token in [
            "/agreements/",
            "/comments",
            "/companies",
            "/count",
            "/lawyers/profile",
            "/posts",
            "/resumes",
            "/salary-statistic",
            "/spec-ups",
        ]
    ):
        return False
    return False


def _infer_allowed_roles_for_endpoint(method: str, path: str) -> tuple[list[str], str]:
    lowered = str(path or "").lower()
    normalized_method = str(method or "").upper()
    allowed_roles: list[str] = []
    reason = "protected_route_inferred"

    if lowered.startswith("/admin"):
        return ["Admin"], "admin_prefix"

    if lowered in {
        "/api/v1/auth/agreements/histories",
        "/api/v1/auth/verifications",
        "/api/v1/lawyer/lawyers/me",
        "/api/v1/lawyer/lawyers/profile",
    }:
        return ["User", "Lawyer", "Company Manager", "Admin"], "authenticated_any_path"

    if lowered == "/api/v1/auth/verifications/:verificationid/files":
        return [], "protected_path_without_confident_role"

    if any(
        token in lowered
        for token in [
            "/companies/me",
            "/companies/members/me",
            "/job-descriptions/me",
            "/job-applications/job-descriptions/",
            "/unread-count",
            "/salary-statistic",
        ]
    ):
        return ["Company Manager"], "company_manager_path"

    if any(
        token in lowered
        for token in [
            "/lawyers/me",
            "/lawyers/profile",
            "/resumes",
            "/notifications",
            "/scraps",
            "/job-applications/me",
            "/comments",
            "/posts",
            "/spec-ups",
        ]
    ):
        return ["Lawyer"], "lawyer_path"

    if any(token in lowered for token in ["/companies", "/job-descriptions/count"]):
        reason = "protected_path_without_confident_role"

    if normalized_method in {"POST", "PUT", "PATCH", "DELETE"} and not allowed_roles:
        if any(token in lowered for token in ["/bookmark", "/scrap", "/like", "/comment", "/apply", "/verification", "/resume", "/post", "/spec-up", "/job-description"]):
            reason = "protected_write_without_confident_role"

    return allowed_roles, reason


def _normalize_role_name(value: object) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if any(token in lowered for token in ["admin", "administrator"]):
        return "Admin"
    if any(token in lowered for token in ["lawyer", "attorney"]):
        return "Lawyer"
    if any(token in lowered for token in ["company", "manager", "employer", "firm", "business"]):
        return "Company Manager"
    if any(token == lowered for token in ["user", "member", "customer"]):
        return "User"
    return text[:1].upper() + text[1:] if text else ""


def _is_probable_plaintext_password(value: object) -> bool:
    text = str(value or "").strip()
    if not text or len(text) > 80:
        return False
    if text.startswith("$2") or text.startswith("argon2") or text.startswith("sha"):
        return False
    return True


def _infer_auth_credential_candidates(seed_jobs: list[dict]) -> list[dict]:
    candidates: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    for job in seed_jobs:
        rows = job.get("rows") if isinstance(job.get("rows"), list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue

            identifier_field = ""
            identifier_value = ""
            password_field = ""
            password_value = ""
            role_value = ""
            for key, value in row.items():
                lowered = str(key).lower()
                if not identifier_field and lowered in {"email", "loginid", "login_id", "username", "user_name"}:
                    identifier_field = str(key)
                    identifier_value = str(value or "").strip()
                if not password_field and lowered in {"password", "passwd", "passcode"} and _is_probable_plaintext_password(value):
                    password_field = str(key)
                    password_value = str(value or "").strip()
                if not role_value and lowered in {"role", "userrole", "user_role", "membertype", "member_type", "usertype", "user_type"}:
                    role_value = _normalize_role_name(value)

            if not identifier_field or not identifier_value or not password_field or not password_value:
                continue

            role = role_value
            model_name = str(job.get("model") or "").lower()
            if not role:
                if "admin" in model_name:
                    role = "Admin"
                elif "lawyer" in model_name:
                    role = "Lawyer"
                elif "company" in model_name or "manager" in model_name:
                    role = "Company Manager"

            if not role:
                continue

            key = (role, identifier_value, password_value)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "role": role,
                    "identifier_field": identifier_field,
                    "identifier_value": identifier_value,
                    "password_field": password_field,
                    "password_value": password_value,
                    "seed_model": str(job.get("model") or ""),
                }
            )

    return candidates


def _infer_auth_credential_candidates_from_seed_sources(repo_path: Path) -> list[dict]:
    skip_parts = {"node_modules", ".git", "dist", "build", "output", ".next", ".dev-analyzer-tools"}
    candidates: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    password_candidates: list[str] = []

    seed_files = sorted(
        path
        for path in repo_path.rglob("*seed*.ts")
        if path.is_file() and not any(part in skip_parts for part in path.parts)
    )
    seed_files.extend(
        path
        for path in repo_path.rglob("*seed*.js")
        if path.is_file() and not any(part in skip_parts for part in path.parts)
    )

    object_pattern = re.compile(r"\{[^{}]*email\s*:\s*['\"]([^'\"]+)['\"][^{}]*\}", re.DOTALL)
    role_pattern = re.compile(
        r"(?:role|roleType|userRole|memberType|userType)\s*:\s*(?:RoleType\.)?([A-Z_]+|['\"][A-Za-z_ ]+['\"])",
        re.DOTALL,
    )
    code_pattern = re.compile(r"(?:code|memberCode)\s*:\s*['\"]([^'\"]+)['\"]", re.DOTALL)

    for seed_file in seed_files:
        try:
            text = seed_file.read_text(encoding="utf-8")
        except OSError:
            continue

        password_candidates.extend(
            match.group(1).strip()
            for match in re.finditer(r"hash\(\s*['\"]([^'\"]+)['\"]", text)
            if match.group(1).strip()
        )
        password_candidates.extend(
            match.group(1).strip()
            for match in re.finditer(r"password\s*:\s*['\"]([^'\"]+)['\"]", text)
            if match.group(1).strip()
        )

        for object_match in object_pattern.finditer(text):
            block = object_match.group(0)
            email = object_match.group(1).strip()
            role_match = role_pattern.search(block)
            role_raw = role_match.group(1).strip("'\" ") if role_match else ""
            role = _normalize_role_name(role_raw)
            if not role:
                continue

            code_match = code_pattern.search(block)
            member_code = code_match.group(1).strip() if code_match else ""
            candidate = {
                "role": role,
                "identifier_field": "email",
                "identifier_value": email,
                "password_field": "password",
                "seed_model": str(seed_file.relative_to(repo_path)).replace("\\", "/"),
            }
            if member_code:
                candidate["member_code"] = member_code
            candidates.append(candidate)

    normalized_passwords = []
    seen_passwords: set[str] = set()
    for password in password_candidates:
        if not _is_probable_plaintext_password(password):
            continue
        if password in seen_passwords:
            continue
        seen_passwords.add(password)
        normalized_passwords.append(password)

    preferred_password = normalized_passwords[0] if normalized_passwords else ""
    finalized: list[dict] = []
    for candidate in candidates:
        password_value = preferred_password
        if not password_value:
            continue
        key = (
            str(candidate.get("role") or ""),
            str(candidate.get("identifier_value") or ""),
            password_value,
        )
        if key in seen:
            continue
        seen.add(key)
        finalized.append(
            {
                "role": str(candidate.get("role") or ""),
                "identifier_field": "email",
                "identifier_value": str(candidate.get("identifier_value") or ""),
                "password_field": "password",
                "password_value": password_value,
                "seed_model": str(candidate.get("seed_model") or ""),
                "member_code": str(candidate.get("member_code") or ""),
            }
        )

    return finalized


def _infer_auth_credential_candidates_from_postman(repo_path: Path) -> list[dict]:
    collections_dir = repo_path / "tests" / "postman"
    if not collections_dir.is_dir():
        return []

    candidates: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    for collection_file in sorted(collections_dir.glob("*.collection.json")):
        try:
            data = json.loads(collection_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        items = data.get("item") if isinstance(data, dict) else []
        if not isinstance(items, list):
            continue

        stack = list(items)
        while stack:
            item = stack.pop()
            if not isinstance(item, dict):
                continue
            child_items = item.get("item")
            if isinstance(child_items, list):
                stack.extend(child_items)
                continue
            request = item.get("request") if isinstance(item.get("request"), dict) else {}
            method = str(request.get("method") or "").upper()
            url_value = request.get("url")
            url_text = url_value if isinstance(url_value, str) else ""
            if method != "POST" or "/login" not in url_text:
                continue
            body = request.get("body") if isinstance(request.get("body"), dict) else {}
            raw = str(body.get("raw") or "").strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            email = str(payload.get("email") or "").strip()
            password = str(payload.get("password") or "").strip()
            if not email or not _is_probable_plaintext_password(password):
                continue
            lowered_name = str(item.get("name") or "").lower()
            role = ""
            if "admin" in lowered_name:
                role = "Admin"
            elif "lawyer" in lowered_name:
                role = "Lawyer"
            elif "company manager" in lowered_name or "company" in lowered_name:
                role = "Company Manager"
            if not role:
                continue
            key = (role, email, password)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "role": role,
                    "identifier_field": "email",
                    "identifier_value": email,
                    "password_field": "password",
                    "password_value": password,
                    "seed_model": str(collection_file.relative_to(repo_path)).replace("\\", "/"),
                }
            )

    return candidates


def _infer_write_payload_samples_from_postman(repo_path: Path) -> dict[tuple[str, str], dict]:
    collections_dir = repo_path / "tests" / "postman"
    if not collections_dir.is_dir():
        return {}

    samples: dict[tuple[str, str], dict] = {}

    for collection_file in sorted(collections_dir.glob("*.collection.json")):
        try:
            data = json.loads(collection_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        items = data.get("item") if isinstance(data, dict) else []
        if not isinstance(items, list):
            continue

        stack = list(items)
        while stack:
            item = stack.pop()
            if not isinstance(item, dict):
                continue
            child_items = item.get("item")
            if isinstance(child_items, list):
                stack.extend(child_items)
                continue

            request = item.get("request") if isinstance(item.get("request"), dict) else {}
            method = str(request.get("method") or "").upper()
            if method not in {"POST", "PUT", "PATCH", "DELETE"}:
                continue

            endpoint = _normalize_collection_request_url(request.get("url"))
            if not endpoint or "/login" in endpoint.lower():
                continue

            body = request.get("body") if isinstance(request.get("body"), dict) else {}
            raw = str(body.get("raw") or "").strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or not payload:
                continue

            key = (method, endpoint)
            if key not in samples:
                samples[key] = payload

    return samples


def _parse_typescript_route_argument(argument_text: str) -> str:
    text = str(argument_text or "").strip()
    if not text:
        return ""
    match = re.search(r"['\"]([^'\"]*)['\"]", text)
    if match:
        return match.group(1).strip()
    return ""


def _extract_typescript_class_body(content: str, class_name: str) -> str:
    match = re.search(rf"export\s+class\s+{re.escape(class_name)}\s*\{{", content)
    if not match:
        return ""
    start = match.end()
    depth = 1
    index = start
    while index < len(content):
        char = content[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return content[start:index]
        index += 1
    return ""


def _infer_sample_value_from_typescript_type(
    field_type: str,
    decorators: list[str],
    property_name: str,
    class_bodies: dict[str, str],
    depth: int = 0,
    seen: set[str] | None = None,
):
    normalized_type = str(field_type or "").strip()
    lowered_type = normalized_type.lower()
    lowered_name = str(property_name or "").strip().lower()
    decorators_text = " ".join(decorators)

    if depth > 2:
        return None

    if "boolean" in lowered_type or "@isboolean" in decorators_text.lower():
        return True
    if any(token in lowered_type for token in ["number", "bigint"]) or "@isnumber" in decorators_text.lower():
        return 1
    if "[]" in normalized_type or "@isarray" in decorators_text.lower():
        nested_match = re.search(r"@Type\(\(\)\s*=>\s*([A-Za-z0-9_]+)\)", decorators_text)
        if nested_match:
            nested_value = _infer_payload_from_typescript_dto(
                nested_match.group(1),
                class_bodies,
                depth=depth + 1,
                seen=seen,
            )
            return [nested_value] if isinstance(nested_value, dict) and nested_value else []
        return []
    if "@isenum" in decorators_text.lower():
        if "status" in lowered_name:
            return "ACTIVE"
        if "type" in lowered_name:
            return "GENERAL"
        return "VALUE"
    if "string" in lowered_type or "@isstring" in decorators_text.lower():
        string_presets = {
            "email": "user@example.com",
            "password": "Password123!",
            "phone": "01012345678",
            "phonenumber": "01012345678",
            "membercode": "LAW001",
            "url": "https://example.com",
            "s3path": "uploads/sample.pdf",
            "filename": "sample.pdf",
            "name": "sample",
            "title": "sample title",
            "content": "sample content",
            "description": "sample description",
            "summary": "sample summary",
        }
        for token, preset in string_presets.items():
            if token in lowered_name:
                return preset
        return "sample"

    nested_type_match = re.match(r"([A-Za-z0-9_]+)", normalized_type)
    if nested_type_match:
        nested_value = _infer_payload_from_typescript_dto(
            nested_type_match.group(1),
            class_bodies,
            depth=depth + 1,
            seen=seen,
        )
        if isinstance(nested_value, dict) and nested_value:
            return nested_value
    return None


def _infer_payload_from_typescript_dto(
    class_name: str,
    class_bodies: dict[str, str],
    depth: int = 0,
    seen: set[str] | None = None,
) -> dict:
    normalized_class_name = str(class_name or "").strip()
    if not normalized_class_name:
        return {}
    if seen is None:
        seen = set()
    if normalized_class_name in seen or depth > 2:
        return {}

    body = class_bodies.get(normalized_class_name) or ""
    if not body:
        return {}

    seen = set(seen)
    seen.add(normalized_class_name)

    payload: dict[str, object] = {}
    optional_payload: dict[str, object] = {}
    pending_decorators: list[str] = []

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("@"):
            pending_decorators.append(line)
            continue

        property_match = re.match(r"([A-Za-z0-9_]+)(\??):\s*([^;=]+)", line)
        if not property_match:
            pending_decorators = []
            continue

        property_name = property_match.group(1).strip()
        is_optional = property_match.group(2) == "?" or any("@IsOptional" in decorator for decorator in pending_decorators)
        field_type = property_match.group(3).strip()
        sample_value = _infer_sample_value_from_typescript_type(
            field_type,
            pending_decorators,
            property_name,
            class_bodies,
            depth=depth,
            seen=seen,
        )
        pending_decorators = []
        if sample_value is None:
            continue
        if is_optional:
            optional_payload[property_name] = sample_value
        else:
            payload[property_name] = sample_value

    if payload:
        return payload

    # Fall back to a few optional fields when the DTO marks everything optional.
    for key in list(optional_payload.keys())[:3]:
        payload[key] = optional_payload[key]
    return payload


def _infer_write_payload_samples_from_dtos(repo_path: Path) -> dict[tuple[str, str], dict]:
    src_root = repo_path / "apps" / "server" / "src"
    if not src_root.is_dir():
        return {}

    ts_files = sorted(src_root.rglob("*.ts"))
    class_bodies: dict[str, str] = {}
    controller_sources: list[str] = []
    for ts_file in ts_files:
        try:
            content = ts_file.read_text(encoding="utf-8")
        except OSError:
            continue
        if ts_file.name.endswith(".controller.ts"):
            controller_sources.append(content)
        for class_match in re.finditer(r"export\s+class\s+([A-Za-z0-9_]+)\s*\{", content):
            class_name = class_match.group(1)
            if class_name not in class_bodies:
                class_bodies[class_name] = _extract_typescript_class_body(content, class_name)

    samples: dict[tuple[str, str], dict] = {}
    route_pattern = re.compile(
        r"@(?P<method>Post|Put|Patch)\((?P<argument>[^)]*)\)(?P<section>[\s\S]{0,800}?)@Body\(\)\s*[A-Za-z0-9_]+\s*:\s*(?P<dto>[A-Za-z0-9_]+)",
        re.MULTILINE,
    )

    for content in controller_sources:
        controller_match = re.search(r"@Controller\(['\"]([^'\"]+)['\"]\)", content)
        controller_prefix = controller_match.group(1).strip() if controller_match else ""

        for route_match in route_pattern.finditer(content):
            method = str(route_match.group("method") or "").upper()
            route_path = _parse_typescript_route_argument(route_match.group("argument"))
            dto_class_name = str(route_match.group("dto") or "").strip()
            endpoint = _join_route_path(controller_prefix, route_path)
            if not method or not endpoint or not dto_class_name:
                continue
            payload = _infer_payload_from_typescript_dto(dto_class_name, class_bodies)
            if not payload:
                continue
            key = (method, endpoint)
            if key not in samples:
                samples[key] = payload

    return samples


def _canonicalize_write_endpoint(endpoint: str) -> str:
    canonical = str(endpoint or "").strip()
    if not canonical:
        return ""
    canonical = re.sub(r"/\d+(?=/|$)", "/:id", canonical)
    canonical = re.sub(r"/[A-Z]{3}\d{3,}(?=/|$)", "/:memberCode", canonical)
    canonical = re.sub(r"/\{\{[^{}]+\}\}(?=/|$)", "/:var", canonical)
    return canonical


def _find_best_write_payload_sample(
    samples: dict[tuple[str, str], dict],
    method: str,
    endpoint: str,
    original_endpoint: str = "",
) -> dict | None:
    normalized_method = str(method or "").upper()
    endpoint = str(endpoint or "").strip()
    original_endpoint = str(original_endpoint or "").strip()
    if not normalized_method or not endpoint:
        return None

    exact_candidates = [
        endpoint,
        original_endpoint,
        _materialize_endpoint_for_smoke(original_endpoint) if original_endpoint and _is_dynamic_endpoint(original_endpoint) else "",
    ]
    for candidate in exact_candidates:
        key = (normalized_method, candidate)
        if candidate and isinstance(samples.get(key), dict) and samples.get(key):
            return dict(samples[key])

    canonical_targets = {
        _canonicalize_write_endpoint(endpoint),
        _canonicalize_write_endpoint(original_endpoint),
    }
    for (sample_method, sample_endpoint), payload in samples.items():
        if sample_method != normalized_method or not isinstance(payload, dict) or not payload:
            continue
        if _canonicalize_write_endpoint(sample_endpoint) in canonical_targets:
            return dict(payload)
    return None


def _write_route_supports_empty_body(method: str, endpoint: str) -> bool:
    normalized_method = str(method or "").upper()
    if normalized_method == "DELETE":
        return True

    lowered = str(endpoint or "").lower()
    empty_body_tokens = [
        "/like",
        "/scraps",
        "/copy",
        "/default",
        "/read",
        "/close",
        "/approve",
        "/reject",
        "/visibility",
        "/apply-click",
        "/resume-status",
        "/role",
    ]
    return any(token in lowered for token in empty_body_tokens)


def _is_safe_write_success_route(route: dict) -> bool:
    method = str(route.get("method") or "").upper()
    endpoint = str(route.get("endpoint") or "")
    if method == "DELETE":
        return False
    if _write_route_supports_empty_body(method, endpoint):
        return True
    lowered = endpoint.lower()
    return method in {"PUT", "PATCH"} and any(token in lowered for token in ["/me", "/profile", "/members/me"])


def _should_generate_validation_write_case(route: dict) -> bool:
    method = str(route.get("method") or "").upper()
    if method not in {"POST", "PUT", "PATCH"}:
        return False

    endpoint = str(route.get("endpoint") or "").lower()
    request_body_mode = str(route.get("request_body_mode") or "").strip().lower()
    if request_body_mode == "none":
        return False

    # Upload/file attachment style endpoints often accept sparse payloads or derive
    # required state from path/context, so "empty payload must fail" is too noisy.
    if "/files" in endpoint:
        return False

    return True


def _is_trigger_or_side_effect_write_endpoint(method: str, path: str) -> bool:
    if method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False

    lowered = path.lower()
    side_effect_tokens = [
        "/trigger",
        "/refresh",
        "/sync",
        "/rebuild",
        "/publish",
        "/reindex",
        "/recalculate",
        "/batch",
    ]
    return any(token in lowered for token in side_effect_tokens)


def _collect_auth_credential_candidates(repo_path: Path, seed_jobs: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    for source_candidates in [
        _infer_auth_credential_candidates(seed_jobs),
        _infer_auth_credential_candidates_from_seed_sources(repo_path),
        _infer_auth_credential_candidates_from_postman(repo_path),
    ]:
        for candidate in source_candidates:
            key = (
                str(candidate.get("role") or ""),
                str(candidate.get("identifier_value") or ""),
                str(candidate.get("password_value") or ""),
            )
            if not all(key) or key in seen:
                continue
            seen.add(key)
            merged.append(candidate)

    return merged


def _build_auto_test_blueprint_heuristic(repo_path: Path, base_url: str, seed_jobs: list[dict]) -> dict:
    endpoints = _discover_project_api_endpoints(repo_path)
    credentials = _collect_auth_credential_candidates(repo_path, seed_jobs)
    write_payload_samples = _infer_write_payload_samples_from_dtos(repo_path)
    for key, payload in _infer_write_payload_samples_from_postman(repo_path).items():
        if key not in write_payload_samples:
            write_payload_samples[key] = payload
    roles = [candidate["role"] for candidate in credentials]
    public_routes: list[dict] = []
    protected_routes: list[dict] = []
    write_routes: list[dict] = []
    skipped_routes: list[dict] = []
    login_route = None

    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        method = str(endpoint.get("method") or "").upper()
        path = str(endpoint.get("endpoint") or "")
        if not method or not path:
            continue

        lowered = path.lower()
        if method == "POST" and login_route is None and any(token in lowered for token in ["/login", "/sign-in", "/signin", "/token/login"]):
            login_route = {"method": "POST", "endpoint": path}
            continue

        if _is_callback_or_integration_endpoint(path):
            skipped_routes.append({"method": method, "endpoint": path, "reason": "callback_or_external"})
            continue

        if _is_trigger_or_side_effect_write_endpoint(method, path):
            skipped_routes.append({"method": method, "endpoint": path, "reason": "write_trigger_or_side_effect"})
            continue

        materialized_from_dynamic = _is_dynamic_endpoint(path)
        smoke_path = _materialize_endpoint_for_smoke(path) if materialized_from_dynamic else path

        if method == "GET":
            if _looks_public_endpoint(smoke_path):
                public_item = {"method": method, "endpoint": smoke_path, "reason": "safe_public_get"}
                if materialized_from_dynamic:
                    public_item["materialized_from_dynamic"] = True
                    public_item["original_endpoint"] = path
                public_routes.append(public_item)
                continue

            allowed_roles, reason = _infer_allowed_roles_for_endpoint(method, path)
            route_item = {"method": method, "endpoint": smoke_path, "reason": reason}
            if materialized_from_dynamic:
                route_item["materialized_from_dynamic"] = True
                route_item["original_endpoint"] = path
            if allowed_roles:
                route_item["allowed_roles"] = allowed_roles
            protected_routes.append(route_item)
            continue

        if method not in {"POST", "PUT", "PATCH", "DELETE"}:
            continue

        allowed_roles, reason = _infer_allowed_roles_for_endpoint(method, path)
        if not allowed_roles:
            skipped_routes.append({"method": method, "endpoint": path, "reason": "write_route_without_confident_role"})
            continue

        route_item = {"method": method, "endpoint": smoke_path, "reason": reason}
        if materialized_from_dynamic:
            route_item["materialized_from_dynamic"] = True
            route_item["original_endpoint"] = path
        route_item["allowed_roles"] = allowed_roles
        sample_payload = _find_best_write_payload_sample(write_payload_samples, method, smoke_path, path)
        if isinstance(sample_payload, dict) and sample_payload:
            route_item["sample_payload"] = sample_payload
        if _write_route_supports_empty_body(method, smoke_path):
            route_item["request_body_mode"] = "none"
        if _is_safe_write_success_route(route_item):
            route_item["safe_write_success"] = True
        write_routes.append(route_item)

    return {
        "version": AUTO_TEST_BLUEPRINT_VERSION,
        "source": "heuristic",
        "base_url": base_url,
        "public_routes": public_routes,
        "login_route": login_route,
        "credentials": credentials,
        "protected_routes": protected_routes,
        "write_routes": write_routes,
        "skipped_routes": skipped_routes,
        "roles": sorted({role for role in roles if role}),
    }


def _build_auto_test_blueprint_prompt(base_url: str, endpoints: list[dict], credentials: list[dict], heuristic: dict) -> str:
    endpoint_samples = []
    for endpoint in endpoints[:250]:
        if not isinstance(endpoint, dict):
            continue
        path = str(endpoint.get("endpoint") or "")
        lowered = path.lower()
        endpoint_samples.append(
            {
                "method": str(endpoint.get("method") or "").upper(),
                "endpoint": path,
                "source": str(endpoint.get("source") or ""),
                "path_hints": {
                    "is_admin": lowered.startswith("/admin"),
                    "is_dynamic": _is_dynamic_endpoint(path),
                    "looks_callback_or_external": _is_callback_or_integration_endpoint(path),
                    "heuristic_public": _looks_public_endpoint(path),
                },
            }
        )

    credential_samples = [item for item in credentials[:20] if isinstance(item, dict)]

    payload = {
        "base_url": base_url,
        "endpoints": endpoint_samples,
        "credential_candidates": credential_samples,
        "heuristic_guess": {
            "login_route": heuristic.get("login_route"),
            "public_routes": heuristic.get("public_routes", [])[:80],
            "protected_routes": heuristic.get("protected_routes", [])[:80],
            "skipped_routes": heuristic.get("skipped_routes", [])[:80],
        },
        "instructions": {
            "goal": "Create a practical API smoke-test blueprint for automatic Postman/Newman generation.",
            "rules": [
                "Return JSON only.",
                "Do not collapse coverage to only health or docs when multiple safe routes are visible.",
                "Use the heuristic guess as a baseline, then improve it where you have better judgment.",
                "Prefer keeping safe heuristic routes unless you have a clear reason to move them to skipped_routes.",
                "Use one of these exact role labels when possible: Public, Admin, Lawyer, Company Manager.",
                "For protected GET routes, set allowed_roles to the minimum confident set.",
                "If a route is probably protected but login credentials are not confidently available, still keep it in protected_routes.",
                "If login credentials are insufficient, return an empty credentials list.",
                "Include a confidence number from 0.0 to 1.0 for each public or protected route.",
                "Add a short reason for why each route is public, protected, or skipped.",
            ],
            "response_schema": {
                "source": "llm",
                "login_route": {"method": "POST", "endpoint": "/api/v1/auth/login"},
                "credentials": [
                    {
                        "role": "Admin",
                        "identifier_field": "email",
                        "identifier_value": "admin@example.com",
                        "password_field": "password",
                        "password_value": "password123",
                    }
                ],
                "public_routes": [{"method": "GET", "endpoint": "/health", "reason": "public_healthcheck", "confidence": 0.99}],
                "protected_routes": [{"method": "GET", "endpoint": "/admin/v1/users", "allowed_roles": ["Admin"], "reason": "admin_prefix", "confidence": 0.95}],
                "skipped_routes": [{"method": "GET", "endpoint": "/api/v1/auth/kakao/callback", "reason": "callback", "confidence": 0.98}],
            },
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _load_json_object(text: str) -> dict | None:
    candidate = str(text or "").strip()
    if not candidate:
        return None
    candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\s*```$", "", candidate)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _build_auto_test_blueprint_with_llm(base_url: str, endpoints: list[dict], credentials: list[dict], heuristic: dict) -> dict | None:
    if not _is_auto_test_llm_enabled() or OpenAI is None:
        return None
    api_key = _get_openai_api_key()
    if not api_key:
        return None

    prompt = _build_auto_test_blueprint_prompt(base_url, endpoints, credentials, heuristic)
    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=_get_auto_test_model(),
            messages=[
                {
                    "role": "system",
                    "content": "You generate conservative API test blueprints. Reply with JSON only.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=3000,
        )
    except Exception as error:
        print(f"    · LLM 기반 테스트 청사진 생성 실패, 휴리스틱으로 대체합니다: {error}")
        return None

    content = ""
    if response.choices:
        content = str(response.choices[0].message.content or "")
    data = _load_json_object(content)
    if not data:
        print("    · LLM 응답을 JSON으로 해석하지 못해 휴리스틱으로 대체합니다.")
        return None
    data["source"] = "llm"
    return data


def _sanitize_role_name_for_variable(role: str) -> str:
    lowered = role.lower()
    if lowered == "admin":
        return "admin"
    if lowered == "lawyer":
        return "lawyer"
    if lowered == "company manager":
        return "companyManager"
    return re.sub(r"[^a-z0-9]+", "", lowered) or "user"


def _postman_test_event(lines: list[str]) -> list[dict]:
    return [
        {
            "listen": "test",
            "script": {
                "type": "text/javascript",
                "exec": lines,
            },
        }
    ]


def _build_login_item(login_route: dict, credential: dict) -> dict:
    role = str(credential.get("role") or "User")
    variable_prefix = _sanitize_role_name_for_variable(role)
    token_variable = f"{variable_prefix}AccessToken"
    identifier_field = str(credential.get("identifier_field") or "email")
    password_field = str(credential.get("password_field") or "password")
    payload = {
        identifier_field: credential.get("identifier_value"),
        password_field: credential.get("password_value"),
    }
    test_lines = [
        f'pm.test("status code is 200 for {role.lower()} login", function () {{',
        "    pm.response.to.have.status(200);",
        "});",
        f'pm.test("response includes access token for {role.lower()} login", function () {{',
        "    const json = pm.response.json();",
        "    pm.expect(json).to.have.property('accessToken');",
        f"    pm.collectionVariables.set('{token_variable}', json.accessToken);",
        "});",
    ]
    return {
        "name": f"{login_route['method']} {login_route['endpoint']} as {role.lower()} returns bearer token",
        "request": {
            "method": login_route["method"],
            "header": [{"key": "Content-Type", "value": "application/json"}],
            "body": {
                "mode": "raw",
                "raw": json.dumps(payload, ensure_ascii=False),
            },
            "url": "{{baseUrl}}" + login_route["endpoint"],
        },
        "event": _postman_test_event(test_lines),
    }


def _build_public_get_item(route: dict) -> dict:
    endpoint = str(route.get("endpoint") or "")
    if endpoint in {"/health", "/docs", "/swagger", "/openapi.json"}:
        name = f"GET {endpoint} is reachable"
    else:
        name = f"GET {endpoint} with public access is reachable"
    return {
        "name": name,
        "request": {
            "method": "GET",
            "url": "{{baseUrl}}" + endpoint,
        },
        "event": _postman_test_event(
            [
                'pm.test("status code is 200 or 204", function () {',
                "    pm.expect([200, 204]).to.include(pm.response.code);",
                "});",
            ]
        ),
    }


def _build_unauthorized_get_item(route: dict) -> dict:
    endpoint = str(route.get("endpoint") or "")
    return {
        "name": f"GET {endpoint} without token returns protected response",
        "request": {
            "method": "GET",
            "url": "{{baseUrl}}" + endpoint,
        },
        "event": _postman_test_event(
            [
                'pm.test("status code is protected without token", function () {',
                "    pm.expect([401, 403, 404]).to.include(pm.response.code);",
                "});",
            ]
        ),
    }


def _to_pascal_case(value: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", str(value or ""))
    return "".join(part[:1].upper() + part[1:] for part in parts if part)


def _infer_route_role_prefix(route: dict, role: str = "") -> str:
    normalized = _sanitize_role_name_for_variable(role) if role else ""
    if normalized:
        return normalized
    allowed_roles = route.get("allowed_roles") if isinstance(route.get("allowed_roles"), list) else []
    if allowed_roles:
        return _sanitize_role_name_for_variable(str(allowed_roles[0]))
    lowered = str(route.get("endpoint") or "").lower()
    if lowered.startswith("/admin"):
        return "admin"
    return "resource"


def _infer_placeholder_name_from_endpoint(endpoint: str) -> str:
    lowered = endpoint.lower()
    if "/auth/members" in lowered:
        return "memberCode"
    if "/verifications" in lowered:
        return "verificationId"
    if "/companies" in lowered:
        return "companyId"
    if "/job-descriptions" in lowered:
        return "jdId"
    if "/posts" in lowered:
        return "postId"
    if "/resumes" in lowered:
        return "resumeId"
    if "/spec-ups" in lowered:
        return "specUpId"
    if "/comments" in lowered:
        return "commentId"
    return "id"


def _build_route_variable_spec(route: dict, role: str = "") -> dict | None:
    original_endpoint = str(route.get("original_endpoint") or "").strip()
    endpoint = str(route.get("endpoint") or "").strip()
    if not original_endpoint or not _is_dynamic_endpoint(original_endpoint):
        return None

    placeholder_name = ""
    matches = re.findall(r":([A-Za-z0-9_]+)", original_endpoint)
    if matches:
        placeholder_name = matches[-1]
    if not placeholder_name:
        return None

    role_prefix = _infer_route_role_prefix(route, role)
    variable_name = f"{role_prefix}{_to_pascal_case(placeholder_name)}"

    keys = [placeholder_name]
    if placeholder_name.lower().endswith("id"):
        keys.append("id")

    fallback_value = ""
    original_parts = [part for part in original_endpoint.strip("/").split("/") if part]
    endpoint_parts = [part for part in endpoint.strip("/").split("/") if part]
    if len(original_parts) == len(endpoint_parts):
        for original_part, endpoint_part in zip(original_parts, endpoint_parts):
            if original_part == f":{placeholder_name}":
                fallback_value = endpoint_part
                break

    return {
        "placeholder_name": placeholder_name,
        "variable_name": variable_name,
        "keys": keys,
        "fallback_value": fallback_value,
    }


def _build_list_route_variable_specs(route: dict, role: str = "") -> list[dict]:
    endpoint = str(route.get("endpoint") or "").strip().lower()
    role_prefix = _infer_route_role_prefix(route, role)

    def spec(placeholder_name: str, keys: list[str]) -> dict:
        return {
            "placeholder_name": placeholder_name,
            "variable_name": f"{role_prefix}{_to_pascal_case(placeholder_name)}",
            "keys": keys,
        }

    mapping = {
        "/admin/v1/auth/members": [spec("memberCode", ["memberCode"])],
        "/admin/v1/auth/verifications": [spec("verificationId", ["verificationId", "id"])],
        "/admin/v1/lawyer/companies": [spec("companyId", ["companyId", "id"])],
        "/admin/v1/lawyer/job-descriptions": [spec("jdId", ["jdId", "id"])],
        "/admin/v1/lawyer/posts": [spec("postId", ["postId", "id"])],
        "/admin/v1/lawyer/resumes": [spec("resumeId", ["resumeId", "id"])],
        "/admin/v1/lawyer/spec-ups": [spec("specUpId", ["specUpId", "id"])],
        "/api/v1/lawyer/posts": [spec("postId", ["postId", "id"])],
        "/api/v1/lawyer/resumes": [spec("resumeId", ["resumeId", "id"])],
        "/api/v1/lawyer/spec-ups": [spec("specUpId", ["specUpId", "id"])],
        "/api/v1/lawyer/job-descriptions/me": [spec("jdId", ["jdId", "id"])],
    }
    return mapping.get(endpoint, [])


def _render_route_endpoint_for_request(route: dict, role: str = "") -> str:
    original_endpoint = str(route.get("original_endpoint") or "").strip()
    endpoint = str(route.get("endpoint") or "").strip()
    if not original_endpoint or not _is_dynamic_endpoint(original_endpoint):
        return endpoint

    spec = _build_route_variable_spec(route, role)
    if not spec:
        return endpoint

    rendered = re.sub(
        r":([A-Za-z0-9_]+)",
        lambda match: f"{{{{{spec['variable_name']}}}}}" if match.group(1) == spec["placeholder_name"] else match.group(0),
        original_endpoint,
    )
    return rendered


def _build_route_extractor_lines(route: dict, role: str = "") -> list[str]:
    endpoint = str(route.get("endpoint") or "")
    original_endpoint = str(route.get("original_endpoint") or "")
    if _is_dynamic_endpoint(original_endpoint):
        return []
    if any(token in endpoint.lower() for token in ["/me", "/count", "/search", "/existence-check", "/popular", "/insights"]):
        if endpoint.lower() != "/api/v1/lawyer/job-descriptions/me":
            return []

    specs = _build_list_route_variable_specs(route, role)
    if not specs:
        return []

    js_specs = []
    for spec in specs:
        placeholder_name = str(spec.get("placeholder_name") or "")
        validator = "value !== undefined && value !== null && String(value).trim().length > 0"
        if placeholder_name == "memberCode":
            validator = "typeof value === 'string' && /^[A-Z]{3}\\d{3,}$/.test(value.trim())"
        elif placeholder_name.lower().endswith("id"):
            validator = "value !== undefined && value !== null && /^\\d+$/.test(String(value).trim())"
        js_specs.append(
            {
                "keys": list(spec.get("keys", [])),
                "variable_name": str(spec.get("variable_name") or ""),
                "validator": validator,
            }
        )

    specs_json = json.dumps(js_specs, ensure_ascii=False)
    return [
        "try {",
        "    const json = pm.response.json();",
        "    const queue = [json];",
        f"    const variableSpecs = {specs_json};",
        "    const foundValues = {};",
        "    while (queue.length) {",
        "        const current = queue.shift();",
        "        if (Array.isArray(current)) {",
        "            for (const item of current) queue.push(item);",
        "            continue;",
        "        }",
        "        if (current && typeof current === 'object') {",
        "            const preferredKeys = ['content', 'items', 'rows', 'data', 'result', 'list'];",
        "            for (const preferredKey of preferredKeys) {",
        "                if (current[preferredKey] !== undefined) {",
        "                    queue.unshift(current[preferredKey]);",
        "                }",
        "            }",
        "            for (const variableSpec of variableSpecs) {",
        "                if (foundValues[variableSpec.variable_name] !== undefined) continue;",
        "                const isValidCandidate = (value) => eval(variableSpec.validator);",
        "                for (const key of variableSpec.keys) {",
        "                    if (isValidCandidate(current[key])) {",
        "                        foundValues[variableSpec.variable_name] = current[key];",
        "                        break;",
        "                    }",
        "                }",
        "            }",
        "            if (Object.keys(foundValues).length === variableSpecs.length) {",
        "                break;",
        "            }",
        "            for (const value of Object.values(current)) queue.push(value);",
        "        }",
        "    }",
        "    for (const variableSpec of variableSpecs) {",
        "        if (foundValues[variableSpec.variable_name] !== undefined) {",
        "            pm.collectionVariables.set(variableSpec.variable_name, String(foundValues[variableSpec.variable_name]));",
        "                }",
        "    }",
        "} catch (error) {}",
    ]


def _build_authorized_get_item(route: dict, role: str) -> dict:
    endpoint = _render_route_endpoint_for_request(route, role)
    variable_prefix = _sanitize_role_name_for_variable(role)
    token_variable = f"{variable_prefix}AccessToken"
    accepted_codes = [200, 204]
    if route.get("materialized_from_dynamic"):
        accepted_codes.append(404)
    accepted_codes_text = ", ".join(str(code) for code in accepted_codes)
    test_lines = [
        f'pm.test("status code is 200 or 204 with {role.lower()} bearer token", function () {{',
        f"    pm.expect([{accepted_codes_text}]).to.include(pm.response.code);",
        "});",
    ]
    test_lines.extend(_build_route_extractor_lines(route, role))
    return {
        "name": f"GET {endpoint} with {role.lower()} bearer token returns reachable response",
        "request": {
            "method": "GET",
            "header": [{"key": "Authorization", "value": f"Bearer {{{{{token_variable}}}}}"}],
            "url": "{{baseUrl}}" + endpoint,
        },
        "event": _postman_test_event(test_lines),
    }


def _build_forbidden_get_item(route: dict, role: str) -> dict:
    endpoint = str(route.get("endpoint") or "")
    variable_prefix = _sanitize_role_name_for_variable(role)
    token_variable = f"{variable_prefix}AccessToken"
    return {
        "name": f"GET {endpoint} with {role.lower()} bearer token returns forbidden",
        "request": {
            "method": "GET",
            "header": [{"key": "Authorization", "value": f"Bearer {{{{{token_variable}}}}}"}],
            "url": "{{baseUrl}}" + endpoint,
        },
        "event": _postman_test_event(
            [
                f'pm.test("status code is forbidden or unauthorized with {role.lower()} bearer token", function () {{',
                "    pm.expect([401, 403, 404]).to.include(pm.response.code);",
                "});",
            ]
        ),
    }


def _normalize_blueprint_routes(routes: object, key_name: str = "allowed_roles") -> list[dict]:
    normalized: list[dict] = []
    if not isinstance(routes, list):
        return normalized
    for route in routes:
        if not isinstance(route, dict):
            continue
        method = str(route.get("method") or "").upper()
        endpoint = str(route.get("endpoint") or "").strip()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"} or not endpoint:
            continue
        item = {"method": method, "endpoint": endpoint}
        if key_name in route and isinstance(route.get(key_name), list):
            item[key_name] = [str(value).strip() for value in route.get(key_name) if str(value).strip()]
        if route.get("reason"):
            item["reason"] = str(route.get("reason"))
        if route.get("materialized_from_dynamic"):
            item["materialized_from_dynamic"] = True
        if route.get("original_endpoint"):
            item["original_endpoint"] = str(route.get("original_endpoint"))
        if route.get("confidence") is not None:
            try:
                item["confidence"] = float(route.get("confidence"))
            except (TypeError, ValueError):
                pass
        if isinstance(route.get("sample_payload"), dict) and route.get("sample_payload"):
            item["sample_payload"] = dict(route.get("sample_payload"))
        request_body_mode = str(route.get("request_body_mode") or "").strip().lower()
        if request_body_mode in {"json", "none"}:
            item["request_body_mode"] = request_body_mode
        if route.get("safe_write_success") is True:
            item["safe_write_success"] = True
        normalized.append(item)
    return normalized


def _normalize_blueprint_credentials(credentials: object) -> list[dict]:
    normalized: list[dict] = []
    if not isinstance(credentials, list):
        return normalized
    for credential in credentials:
        if not isinstance(credential, dict):
            continue
        role = _normalize_role_name(credential.get("role"))
        identifier_field = str(credential.get("identifier_field") or "").strip()
        identifier_value = str(credential.get("identifier_value") or "").strip()
        password_field = str(credential.get("password_field") or "").strip()
        password_value = str(credential.get("password_value") or "").strip()
        if not all([role, identifier_field, identifier_value, password_field, password_value]):
            continue
        normalized.append(
            {
                "role": role,
                "identifier_field": identifier_field,
                "identifier_value": identifier_value,
                "password_field": password_field,
                "password_value": password_value,
            }
        )
    return normalized


def _merge_route_lists(heuristic_routes: list[dict], llm_routes: list[dict], key_name: str = "allowed_roles") -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}

    for route in heuristic_routes:
        if not isinstance(route, dict):
            continue
        key = (str(route.get("method") or "").upper(), str(route.get("endpoint") or "").strip())
        if not key[0] or not key[1]:
            continue
        merged[key] = dict(route)

    for route in llm_routes:
        if not isinstance(route, dict):
            continue
        key = (str(route.get("method") or "").upper(), str(route.get("endpoint") or "").strip())
        if not key[0] or not key[1]:
            continue
        existing = dict(merged.get(key) or {})
        candidate = dict(route)
        materialized_key = (key[0], _materialize_endpoint_for_smoke(key[1])) if _is_dynamic_endpoint(key[1]) else None
        confidence = candidate.get("confidence")
        try:
            confidence_value = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            confidence_value = None

        if materialized_key and materialized_key in merged:
            continue

        if not existing:
            merged[key] = candidate
            continue

        if confidence_value is not None and confidence_value < 0.45:
            continue

        existing_reason = str(existing.get("reason") or "").strip()
        candidate_reason = str(candidate.get("reason") or "").strip()
        if candidate_reason:
            existing["reason"] = candidate_reason
        elif existing_reason:
            existing["reason"] = existing_reason

        if key_name in candidate and isinstance(candidate.get(key_name), list) and candidate.get(key_name):
            existing[key_name] = [str(value).strip() for value in candidate.get(key_name) if str(value).strip()]

        if confidence_value is not None:
            existing["confidence"] = confidence_value
        if candidate.get("materialized_from_dynamic"):
            existing["materialized_from_dynamic"] = True
        if candidate.get("original_endpoint"):
            existing["original_endpoint"] = str(candidate.get("original_endpoint"))

        merged[key] = existing

    return sorted(merged.values(), key=lambda item: (str(item.get("endpoint") or ""), str(item.get("method") or "")))


def _finalize_auto_test_blueprint(base_url: str, llm_blueprint: dict | None, heuristic_blueprint: dict) -> dict:
    if not isinstance(llm_blueprint, dict):
        heuristic_blueprint["base_url"] = base_url
        return heuristic_blueprint

    login_route = llm_blueprint.get("login_route") if isinstance(llm_blueprint.get("login_route"), dict) else None
    if login_route:
        login_method = str(login_route.get("method") or "").upper()
        login_endpoint = str(login_route.get("endpoint") or "").strip()
        if login_method != "POST" or not login_endpoint:
            login_route = None

    credentials = _normalize_blueprint_credentials(llm_blueprint.get("credentials"))
    public_routes = _normalize_blueprint_routes(llm_blueprint.get("public_routes"))
    protected_routes = _normalize_blueprint_routes(llm_blueprint.get("protected_routes"))
    write_routes = _normalize_blueprint_routes(llm_blueprint.get("write_routes"))
    skipped_routes = _normalize_blueprint_routes(llm_blueprint.get("skipped_routes"), key_name="reason")

    public_routes = _merge_route_lists(
        heuristic_blueprint.get("public_routes", []),
        public_routes,
        key_name="reason",
    )
    protected_routes = _merge_route_lists(
        heuristic_blueprint.get("protected_routes", []),
        protected_routes,
    )
    write_routes = _merge_route_lists(
        heuristic_blueprint.get("write_routes", []),
        write_routes,
    )
    skipped_routes = _merge_route_lists(
        heuristic_blueprint.get("skipped_routes", []),
        skipped_routes,
        key_name="reason",
    )
    if not credentials:
        credentials = heuristic_blueprint.get("credentials", [])
    if not login_route:
        login_route = heuristic_blueprint.get("login_route")

    roles = sorted(
        {
            str(item.get("role") or "").strip()
            for item in credentials
            if isinstance(item, dict) and str(item.get("role") or "").strip()
        }
    )
    candidate = {
        "version": int(llm_blueprint.get("version") or heuristic_blueprint.get("version") or AUTO_TEST_BLUEPRINT_VERSION),
        "source": "llm+heuristic",
        "base_url": base_url,
        "login_route": login_route,
        "credentials": credentials,
        "public_routes": public_routes,
        "protected_routes": protected_routes,
        "write_routes": write_routes,
        "skipped_routes": skipped_routes,
        "roles": roles,
    }

    heuristic_public = len(heuristic_blueprint.get("public_routes", []))
    heuristic_protected = len(heuristic_blueprint.get("protected_routes", []))
    heuristic_write = len(heuristic_blueprint.get("write_routes", []))
    candidate_public = len(candidate.get("public_routes", []))
    candidate_protected = len(candidate.get("protected_routes", []))
    candidate_write = len(candidate.get("write_routes", []))
    candidate_total = candidate_public + candidate_protected + candidate_write
    heuristic_total = heuristic_public + heuristic_protected + heuristic_write

    # Guardrail: if the LLM draft is much sparser than the heuristic baseline,
    # keep the conservative heuristic plan to avoid collapsing coverage to 1-2 routes.
    if heuristic_total > 0 and candidate_total < max(3, heuristic_total // 2):
        heuristic_blueprint["base_url"] = base_url
        return heuristic_blueprint
    if heuristic_public > 0 and candidate_public == 0:
        heuristic_blueprint["base_url"] = base_url
        return heuristic_blueprint

    return candidate


def _build_write_request_body(payload: dict | None) -> str:
    return json.dumps(payload or {}, ensure_ascii=False)


def _build_write_request_body_section(route: dict, payload: dict | None = None) -> dict | None:
    request_body_mode = str(route.get("request_body_mode") or "").strip().lower()
    effective_payload = payload if isinstance(payload, dict) else None
    if request_body_mode == "none":
        return None
    if effective_payload is None:
        effective_payload = {}
    return {"mode": "raw", "raw": _build_write_request_body(effective_payload)}


def _build_unauthorized_write_item(route: dict) -> dict:
    endpoint = _render_route_endpoint_for_request(route)
    method = str(route.get("method") or "POST").upper()
    request = {
        "method": method,
        "header": [{"key": "Content-Type", "value": "application/json"}],
        "url": "{{baseUrl}}" + endpoint,
    }
    body = _build_write_request_body_section(route, {})
    if body is not None:
        request["body"] = body
    return {
        "name": f"{method} {endpoint} without token returns protected response",
        "request": request,
        "event": _postman_test_event(
            [
                'pm.test("status code is protected without token", function () {',
                "    pm.expect([401, 403, 404]).to.include(pm.response.code);",
                "});",
            ]
        ),
    }


def _build_validation_write_item(route: dict, role: str) -> dict:
    endpoint = _render_route_endpoint_for_request(route, role)
    method = str(route.get("method") or "POST").upper()
    token_variable = f"{_sanitize_role_name_for_variable(role)}AccessToken"
    request = {
        "method": method,
        "header": [
            {"key": "Content-Type", "value": "application/json"},
            {"key": "Authorization", "value": f"Bearer {{{{{token_variable}}}}}"},
        ],
        "url": "{{baseUrl}}" + endpoint,
    }
    body = _build_write_request_body_section(route, {})
    if body is not None:
        request["body"] = body
    return {
        "name": f"{method} {endpoint} with {role.lower()} bearer token returns validation error for empty payload",
        "request": request,
        "event": _postman_test_event(
            [
                f'pm.test("status code is validation-compatible with {role.lower()} bearer token", function () {{',
                "    pm.expect([400, 401, 403, 404, 415, 422]).to.include(pm.response.code);",
                "});",
            ]
        ),
    }


def _build_authorized_write_item(route: dict, role: str) -> dict:
    endpoint = _render_route_endpoint_for_request(route, role)
    method = str(route.get("method") or "POST").upper()
    token_variable = f"{_sanitize_role_name_for_variable(role)}AccessToken"
    accepted_codes = [200, 201, 202, 204]
    if route.get("materialized_from_dynamic"):
        accepted_codes.append(404)
    accepted_codes_text = ", ".join(str(code) for code in accepted_codes)
    payload = route.get("sample_payload") if isinstance(route.get("sample_payload"), dict) else {}
    request = {
        "method": method,
        "header": [
            {"key": "Content-Type", "value": "application/json"},
            {"key": "Authorization", "value": f"Bearer {{{{{token_variable}}}}}"},
        ],
        "url": "{{baseUrl}}" + endpoint,
    }
    body = _build_write_request_body_section(route, payload)
    if body is not None:
        request["body"] = body
    return {
        "name": f"{method} {endpoint} with {role.lower()} bearer token returns reachable response",
        "request": request,
        "event": _postman_test_event(
            [
                f'pm.test("status code is write-compatible with {role.lower()} bearer token", function () {{',
                f"    pm.expect([{accepted_codes_text}]).to.include(pm.response.code);",
                "});",
            ]
        ),
    }


def _build_forbidden_write_item(route: dict, role: str) -> dict:
    endpoint = _render_route_endpoint_for_request(route, role)
    method = str(route.get("method") or "POST").upper()
    token_variable = f"{_sanitize_role_name_for_variable(role)}AccessToken"
    payload = route.get("sample_payload") if isinstance(route.get("sample_payload"), dict) else {}
    request = {
        "method": method,
        "header": [
            {"key": "Content-Type", "value": "application/json"},
            {"key": "Authorization", "value": f"Bearer {{{{{token_variable}}}}}"},
        ],
        "url": "{{baseUrl}}" + endpoint,
    }
    body = _build_write_request_body_section(route, payload)
    if body is not None:
        request["body"] = body
    return {
        "name": f"{method} {endpoint} with {role.lower()} bearer token returns forbidden",
        "request": request,
        "event": _postman_test_event(
            [
                f'pm.test("status code is forbidden or unauthorized with {role.lower()} bearer token", function () {{',
                "    pm.expect([401, 403, 404]).to.include(pm.response.code);",
                "});",
            ]
        ),
    }


def _build_auto_generated_collection(repo_path: Path, base_url: str) -> tuple[dict, dict]:
    seed_jobs = _load_external_seed_jobs(repo_path)
    heuristic_blueprint = _build_auto_test_blueprint_heuristic(repo_path, base_url, seed_jobs)
    llm_blueprint = _build_auto_test_blueprint_with_llm(
        base_url,
        _discover_project_api_endpoints(repo_path),
        heuristic_blueprint.get("credentials", []),
        heuristic_blueprint,
    )
    blueprint = _finalize_auto_test_blueprint(base_url, llm_blueprint, heuristic_blueprint)

    collection = _build_collection_from_blueprint(base_url, blueprint)
    return collection, blueprint


def _build_collection_from_blueprint(base_url: str, blueprint: dict) -> dict:
    items: list[dict] = []
    variables = [{"key": "baseUrl", "value": base_url}]
    credentials = blueprint.get("credentials", []) if isinstance(blueprint, dict) else []
    login_route = blueprint.get("login_route") if isinstance(blueprint, dict) else None
    variable_defaults: dict[str, str] = {}
    enable_write_success = _is_auto_test_write_success_enabled()

    for role in blueprint.get("roles", []) if isinstance(blueprint, dict) else []:
        if not isinstance(role, str) or not role.strip():
            continue
        token_variable = f"{_sanitize_role_name_for_variable(role)}AccessToken"
        variables.append({"key": token_variable, "value": ""})

    for route in blueprint.get("protected_routes", []) if isinstance(blueprint, dict) else []:
        if not isinstance(route, dict):
            continue
        spec = _build_route_variable_spec(route)
        if not spec:
            continue
        variable_name = str(spec.get("variable_name") or "")
        fallback_value = str(spec.get("fallback_value") or "")
        if variable_name and variable_name not in variable_defaults:
            variable_defaults[variable_name] = fallback_value
    for route in blueprint.get("write_routes", []) if isinstance(blueprint, dict) else []:
        if not isinstance(route, dict):
            continue
        spec = _build_route_variable_spec(route)
        if not spec:
            continue
        variable_name = str(spec.get("variable_name") or "")
        fallback_value = str(spec.get("fallback_value") or "")
        if variable_name and variable_name not in variable_defaults:
            variable_defaults[variable_name] = fallback_value
    for variable_name, fallback_value in sorted(variable_defaults.items()):
        variables.append({"key": variable_name, "value": fallback_value})

    for route in blueprint.get("public_routes", []) if isinstance(blueprint, dict) else []:
        if not isinstance(route, dict):
            continue
        items.append(_build_public_get_item(route))

    if isinstance(login_route, dict):
        for credential in credentials:
            if not isinstance(credential, dict):
                continue
            items.append(_build_login_item(login_route, credential))

    credential_roles = [str(item.get("role") or "") for item in credentials if isinstance(item, dict)]
    for route in blueprint.get("protected_routes", []) if isinstance(blueprint, dict) else []:
        if not isinstance(route, dict):
            continue
        items.append(_build_unauthorized_get_item(route))
        allowed_roles = [role for role in route.get("allowed_roles", []) if role in credential_roles]
        if not allowed_roles:
            continue

        for allowed_role in allowed_roles:
            items.append(_build_authorized_get_item(route, allowed_role))
        denied_role = next((role for role in credential_roles if role not in allowed_roles), "")
        if denied_role:
            items.append(_build_forbidden_get_item(route, denied_role))

    for route in blueprint.get("write_routes", []) if isinstance(blueprint, dict) else []:
        if not isinstance(route, dict):
            continue
        items.append(_build_unauthorized_write_item(route))
        allowed_roles = [role for role in route.get("allowed_roles", []) if role in credential_roles]
        if not allowed_roles:
            continue
        primary_role = allowed_roles[0]
        has_sample_payload = isinstance(route.get("sample_payload"), dict) and route.get("sample_payload")
        if has_sample_payload and _should_generate_validation_write_case(route):
            items.append(_build_validation_write_item(route, primary_role))
        allow_safe_write_success = route.get("safe_write_success") is True
        if enable_write_success and (has_sample_payload or allow_safe_write_success):
            for allowed_role in allowed_roles:
                items.append(_build_authorized_write_item(route, allowed_role))
        denied_role = next((role for role in credential_roles if role not in allowed_roles), "")
        if denied_role:
            items.append(_build_forbidden_write_item(route, denied_role))

    collection = {
        "info": {
            "name": "Auto Generated API Smoke Tests",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "variable": variables,
        "item": items,
    }
    return collection


def _prepare_newman_collection_artifacts(
    repo_path: Path,
    api_test: dict,
    newman_config: dict,
    force_refresh: bool = False,
) -> tuple[dict, Path, dict]:
    prepared = dict(newman_config)
    base_url = str(api_test.get("base_url") or "").strip()
    auto_generate_raw = prepared.get("auto_generate", True)
    if isinstance(auto_generate_raw, str):
        auto_generate = auto_generate_raw.strip().lower() not in {"0", "false", "no", "off"}
    else:
        auto_generate = bool(auto_generate_raw)

    for key in ["collection", "environment"]:
        if prepared.get(key):
            prepared[key] = str(_resolve_repo_relative_path(repo_path, str(prepared[key])))

    generated_dir = repo_path / ".dev-analyzer-tools" / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    generated_collection_path = generated_dir / "auto-smoke.collection.json"
    collection_path = generated_collection_path if auto_generate else Path(str(prepared.get("collection") or generated_collection_path))
    blueprint_path = generated_dir / "auto-test-blueprint.json"

    generation_meta = {
        "mode": "configured",
        "collection_path": str(collection_path),
        "blueprint_path": "",
        "source": "configured",
        "llm_enabled": _is_auto_test_llm_enabled(),
        "reused": False,
        "public_routes": 0,
        "protected_routes": 0,
        "skipped_routes": 0,
    }

    if auto_generate and not force_refresh and blueprint_path.is_file() and collection_path.is_file():
        try:
            blueprint = json.loads(blueprint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            blueprint = {}
        if isinstance(blueprint, dict) and int(blueprint.get("version") or 0) == AUTO_TEST_BLUEPRINT_VERSION:
            prepared["collection"] = str(collection_path)
            generation_meta = {
                "mode": "generated",
                "collection_path": str(collection_path),
                "blueprint_path": str(blueprint_path),
                "source": str(blueprint.get("source") or "heuristic"),
                "version": int(blueprint.get("version") or AUTO_TEST_BLUEPRINT_VERSION),
                "llm_enabled": _is_auto_test_llm_enabled(),
                "reused": True,
                "public_routes": len(blueprint.get("public_routes", [])),
                "protected_routes": len(blueprint.get("protected_routes", [])) + len(blueprint.get("write_routes", [])),
                "skipped_routes": len(blueprint.get("skipped_routes", [])),
            }
            environment_path_text = str(prepared.get("environment") or "").strip()
            if environment_path_text and not Path(environment_path_text).is_file():
                print(f"    · Newman environment 파일을 찾지 못해 옵션을 건너뜁니다: {environment_path_text}")
                prepared.pop("environment", None)
            print(f"    · 기존 자동 테스트 설계 재사용: {blueprint_path}")
            return prepared, collection_path, generation_meta

    if auto_generate or not collection_path.is_file():
        collection, blueprint = _build_auto_generated_collection(repo_path, base_url)
        collection_path = generated_dir / "auto-smoke.collection.json"
        collection_path.write_text(json.dumps(collection, ensure_ascii=False, indent=2), encoding="utf-8")
        blueprint_path.write_text(json.dumps(blueprint, ensure_ascii=False, indent=2), encoding="utf-8")
        prepared["collection"] = str(collection_path)
        generation_meta = {
            "mode": "generated",
            "collection_path": str(collection_path),
            "blueprint_path": str(blueprint_path),
            "source": str(blueprint.get("source") or "heuristic"),
            "version": int(blueprint.get("version") or AUTO_TEST_BLUEPRINT_VERSION),
            "llm_enabled": _is_auto_test_llm_enabled(),
            "reused": False,
            "public_routes": len(blueprint.get("public_routes", [])),
            "protected_routes": len(blueprint.get("protected_routes", [])) + len(blueprint.get("write_routes", [])),
            "skipped_routes": len(blueprint.get("skipped_routes", [])),
        }
        print(f"    · 자동 테스트 컬렉션 생성 완료 ({generation_meta['source']}): {collection_path}")
    else:
        prepared["collection"] = str(collection_path)

    environment_path_text = str(prepared.get("environment") or "").strip()
    if environment_path_text and not Path(environment_path_text).is_file():
        print(f"    · Newman environment 파일을 찾지 못해 옵션을 건너뜁니다: {environment_path_text}")
        prepared.pop("environment", None)

    return prepared, collection_path, generation_meta


def _restore_runtime_env_file(env_path: Path | None, original_content: str | None) -> None:
    if env_path is None:
        return
    try:
        if original_content is None:
            if env_path.exists():
                env_path.unlink()
        else:
            env_path.write_text(original_content, encoding="utf-8")
    except OSError:
        pass


def _read_repo_config_file(path: Path) -> dict:
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    else:
        if yaml is None:
            raise RuntimeError("YAML 설정 파일을 읽으려면 PyYAML이 필요합니다.")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise RuntimeError("분석 설정 파일 형식이 올바르지 않습니다.")
    return data


def _find_repo_config_path(repo_path: Path) -> Path | None:
    preferred_names = [
        ".dev-analyzer.yml",
        ".dev-analyzer.yaml",
        ".dev-analyzer.json",
        "dev-analyzer.yml",
        "dev-analyzer.yaml",
        "dev-analyzer.json",
    ]
    for name in preferred_names:
        path = repo_path / name
        if path.is_file():
            return path

    patterns = [
        ".dev-analyzer.yml",
        ".dev-analyzer.yaml",
        ".dev-analyzer.json",
        "dev-analyzer.yml",
        "dev-analyzer.yaml",
        "dev-analyzer.json",
    ]
    for pattern in patterns:
        matches = sorted(repo_path.rglob(pattern), key=lambda item: (len(item.parts), str(item)))
        if matches:
            return matches[0]
    return None


def _load_repo_config(repo_path: Path) -> dict:
    path = _find_repo_config_path(repo_path)
    if path is None:
        return {}
    return _read_repo_config_file(path)


def get_repo_config_path(repo_path: Path) -> Path | None:
    return _find_repo_config_path(repo_path)


def _deep_merge_dicts(base: dict, overrides: dict) -> dict:
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _is_missing_config_value(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def _fill_missing_config_values(current: object, inferred: object) -> object:
    if isinstance(current, dict) and isinstance(inferred, dict):
        merged = dict(current)
        for key, inferred_value in inferred.items():
            if key not in merged:
                merged[key] = inferred_value
                continue
            merged[key] = _fill_missing_config_values(merged[key], inferred_value)
        return merged

    if isinstance(current, list) and isinstance(inferred, list):
        return inferred if not current else current

    return inferred if _is_missing_config_value(current) else current


def _read_json_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _extract_compose_default(value: object) -> str:
    raw = str(value or "").strip()
    match = re.search(r"\$\{[^:}]+:-([^}]+)\}", raw)
    if match:
        return match.group(1).strip()
    return raw


def _load_default_repo_template() -> dict:
    if not DEFAULT_REPO_CONFIG_TEMPLATE.is_file():
        return {}
    try:
        return _read_repo_config_file(DEFAULT_REPO_CONFIG_TEMPLATE)
    except RuntimeError:
        return {}


def _write_repo_config_file(path: Path, config: dict) -> None:
    if path.suffix == ".json":
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    if yaml is None:
        raise RuntimeError("YAML 설정 파일을 저장하려면 PyYAML이 필요합니다.")
    path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=False), encoding="utf-8")


def _load_compose_config(repo_path: Path) -> dict:
    if yaml is None:
        return {}

    for name in ["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"]:
        path = repo_path / name
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _parse_host_port(ports: object) -> tuple[str, int] | None:
    if isinstance(ports, list):
        for entry in ports:
            if isinstance(entry, dict):
                published = str(entry.get("published") or "").strip()
                host_ip = str(entry.get("host_ip") or "127.0.0.1").strip() or "127.0.0.1"
                if published.isdigit():
                    return host_ip, int(published)
            elif isinstance(entry, str):
                cleaned = entry.strip().strip('"').strip("'")
                parts = cleaned.split(":")
                if len(parts) >= 2:
                    published = parts[-2]
                    host_ip = "127.0.0.1" if len(parts) == 2 else (parts[-3] if len(parts) >= 3 else "127.0.0.1")
                    if published.isdigit():
                        return host_ip, int(published)
                elif cleaned.isdigit():
                    return "127.0.0.1", int(cleaned)
    return None


def _read_package_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    return _read_json_file(path)


def _infer_package_manager(repo_path: Path, package_data: dict) -> str:
    package_manager = str(package_data.get("packageManager") or "").strip().lower()
    if package_manager.startswith("pnpm"):
        return "pnpm"
    if package_manager.startswith("yarn"):
        return "yarn"
    if package_manager.startswith("npm"):
        return "npm"
    if (repo_path / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (repo_path / "yarn.lock").is_file():
        return "yarn"
    return "npm"


def _script_command(package_manager: str, script_name: str) -> str:
    manager = (package_manager or "npm").strip().lower()
    if manager == "pnpm":
        return f"pnpm run {script_name}"
    if manager == "yarn":
        return f"yarn {script_name}"
    return f"npm run {script_name}"


def _infer_startup_config(repo_path: Path) -> dict:
    candidates = [
        repo_path / "apps" / "server" / "package.json",
        repo_path / "server" / "package.json",
        repo_path / "api" / "package.json",
        repo_path / "backend" / "package.json",
        repo_path / "package.json",
    ]
    preferred_scripts = ["dev", "start:dev", "start"]

    for package_json in candidates:
        package_data = _read_package_json(package_json)
        scripts = package_data.get("scripts") if isinstance(package_data.get("scripts"), dict) else {}
        if not scripts:
            continue
        for script_name in preferred_scripts:
            if script_name not in scripts:
                continue
            package_manager = _infer_package_manager(repo_path, package_data)
            start_cwd = package_json.parent.relative_to(repo_path).as_posix() if package_json.parent != repo_path else "."
            return {
                "start_command": _script_command(package_manager, script_name),
                "start_cwd": start_cwd,
                "runtime": {
                    "node_env": "development" if "dev" in script_name else "test",
                },
            }

    return {
        "start_command": "npm run dev",
        "start_cwd": ".",
        "runtime": {
            "node_env": "test",
        },
    }


def _infer_database_type(repo_path: Path, compose_services: dict) -> str:
    schema_path = repo_path / "prisma" / "schema.prisma"
    if schema_path.is_file():
        try:
            schema_text = schema_path.read_text(encoding="utf-8")
        except OSError:
            schema_text = ""
        match = re.search(r'datasource\s+\w+\s*\{[^}]*provider\s*=\s*"([^"]+)"', schema_text, re.DOTALL)
        if match:
            return match.group(1).strip().lower()

    for service in compose_services.values():
        if not isinstance(service, dict):
            continue
        image = str(service.get("image") or "").lower()
        if "postgres" in image:
            return "postgresql"
        if "mariadb" in image:
            return "mariadb"
        if "mysql" in image:
            return "mysql"
    return "postgresql"


def _infer_database_config(repo_path: Path, compose_services: dict) -> dict:
    database_type = _infer_database_type(repo_path, compose_services)
    config = {
        "type": database_type,
        "url": "",
        "host": "127.0.0.1",
        "port": 5432 if database_type in {"postgres", "postgresql"} else 3306,
        "name": "app_test",
        "user": "app",
        "password": "app_password",
        "init": {
            "enabled": (repo_path / "prisma" / "schema.prisma").is_file(),
            "mode": "db_push",
            "seed": False,
        },
    }

    for service_name, service in compose_services.items():
        if not isinstance(service, dict):
            continue
        image = str(service.get("image") or "").lower()
        if service_name not in {"db", "database", "postgres", "mysql", "mariadb"} and not any(
            keyword in image for keyword in ["postgres", "mysql", "mariadb"]
        ):
            continue

        port_info = _parse_host_port(service.get("ports"))
        if port_info is not None:
            host, port = port_info
            config["host"] = host
            config["port"] = port

        environment = service.get("environment") if isinstance(service.get("environment"), dict) else {}
        if database_type in {"postgres", "postgresql"}:
            config["name"] = _extract_compose_default(environment.get("POSTGRES_DB")) or str(config["name"])
            config["user"] = _extract_compose_default(environment.get("POSTGRES_USER")) or str(config["user"])
            config["password"] = _extract_compose_default(environment.get("POSTGRES_PASSWORD")) or str(config["password"])
        else:
            config["name"] = _extract_compose_default(environment.get("MYSQL_DATABASE")) or str(config["name"])
            config["user"] = _extract_compose_default(environment.get("MYSQL_USER")) or str(config["user"])
            config["password"] = _extract_compose_default(environment.get("MYSQL_PASSWORD")) or str(config["password"])
        break

    return config


def _infer_redis_config(compose_services: dict) -> dict:
    config = {
        "host": "127.0.0.1",
        "port": 6379,
    }

    for service_name, service in compose_services.items():
        if not isinstance(service, dict):
            continue
        image = str(service.get("image") or "").lower()
        if service_name != "redis" and "redis" not in image:
            continue
        port_info = _parse_host_port(service.get("ports"))
        if port_info is not None:
            host, port = port_info
            config["host"] = host
            config["port"] = port
        break

    return config


def _infer_docker_services(compose_services: dict) -> list[str]:
    services: list[str] = []
    for service_name, service in compose_services.items():
        if not isinstance(service, dict):
            continue
        image = str(service.get("image") or "").lower()
        if service_name in {"db", "database", "postgres", "mysql", "mariadb", "redis"} or any(
            keyword in image for keyword in ["postgres", "mysql", "mariadb", "redis"]
        ):
            services.append(service_name)
    return services or ["db", "redis"]


def _infer_server_port(compose_services: dict, start_config: dict) -> int:
    server_service = compose_services.get("server") if isinstance(compose_services.get("server"), dict) else {}
    port_info = _parse_host_port(server_service.get("ports")) if server_service else None
    if port_info is not None:
        return port_info[1]

    environment = server_service.get("environment") if isinstance(server_service.get("environment"), dict) else {}
    port = _extract_compose_default(environment.get("PORT"))
    if port.isdigit():
        return int(port)

    runtime = start_config.get("runtime") if isinstance(start_config.get("runtime"), dict) else {}
    runtime_port = str(runtime.get("port") or "").strip()
    if runtime_port.isdigit():
        return int(runtime_port)
    return 3000


def _infer_healthcheck_path(repo_path: Path) -> str:
    health_candidates = [
        repo_path / "apps" / "server" / "src" / "health" / "health.controller.ts",
        repo_path / "src" / "health" / "health.controller.ts",
    ]
    for path in health_candidates:
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        controller_match = re.search(r"@Controller\(['\"]([^'\"]+)['\"]\)", content)
        if controller_match:
            return f"/{controller_match.group(1).strip('/')}"
        return "/health"
    return "/health"


def _infer_newman_config(repo_path: Path) -> dict:
    environment_candidates = sorted(
        repo_path.rglob("*environment*.json"),
        key=lambda item: (len(item.parts), str(item)),
    )

    config: dict[str, object] = {
        "auto_generate": True,
        "collection": ".dev-analyzer-tools/generated/auto-smoke.collection.json",
        "reporters": ["json"],
    }
    if environment_candidates:
        config["environment"] = environment_candidates[0].relative_to(repo_path).as_posix()
    return config


def _build_inferred_repo_config(repo_path: Path) -> dict:
    base_config = _load_default_repo_template()
    compose_config = _load_compose_config(repo_path)
    compose_services = compose_config.get("services") if isinstance(compose_config.get("services"), dict) else {}
    start_config = _infer_startup_config(repo_path)
    port = _infer_server_port(compose_services, start_config)
    runtime = dict(start_config.get("runtime") or {})
    runtime["port"] = port

    inferred_config = {
        "api_test": {
            "enabled": True,
            "runner": "newman",
            "start_command": start_config.get("start_command", "npm run dev"),
            "start_cwd": start_config.get("start_cwd", "."),
            "base_url": f"http://127.0.0.1:{port}",
            "runtime": runtime,
            "database": _infer_database_config(repo_path, compose_services),
            "redis": _infer_redis_config(compose_services),
            "docker": {
                "services": _infer_docker_services(compose_services),
                "cleanup": "keep",
            },
            "healthcheck": {
                "path": _infer_healthcheck_path(repo_path),
                "timeout_seconds": 120,
                "interval_seconds": 3,
            },
            "env": {
                "NODE_ENV": runtime.get("node_env", "test"),
                "PORT": str(port),
            },
            "newman": _infer_newman_config(repo_path),
        }
    }

    return _deep_merge_dicts(base_config, inferred_config)


def ensure_repo_config_exists(repo_path: Path) -> Path | None:
    config_path = _find_repo_config_path(repo_path)
    if config_path is not None:
        try:
            existing_config = _read_repo_config_file(config_path)
        except RuntimeError:
            return config_path

        inferred_config = _build_inferred_repo_config(repo_path)
        if inferred_config:
            merged_config = _fill_missing_config_values(existing_config, inferred_config)
            if merged_config != existing_config:
                _write_repo_config_file(config_path, merged_config)
        return config_path

    target_path = repo_path / ".dev-analyzer.yml"
    if yaml is None:
        if not DEFAULT_REPO_CONFIG_TEMPLATE.is_file():
            return None
        target_path.write_text(DEFAULT_REPO_CONFIG_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
        return target_path

    config = _build_inferred_repo_config(repo_path)
    if not config:
        if not DEFAULT_REPO_CONFIG_TEMPLATE.is_file():
            return None
        target_path.write_text(DEFAULT_REPO_CONFIG_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
        return target_path

    _write_repo_config_file(target_path, config)
    return target_path


def has_api_test_config(repo_path: Path) -> bool:
    api_test = _load_repo_config(repo_path).get("api_test")
    return isinstance(api_test, dict) and bool(api_test.get("enabled", True))


def _resolve_repo_relative_path(repo_path: Path, value: str | None, default: str = ".") -> Path:
    path = Path(value or default)
    if not path.is_absolute():
        path = repo_path / path
    return path.resolve()


def _has_eslint_config(repo_path: Path) -> bool:
    config_names = [
        "eslint.config.js",
        "eslint.config.mjs",
        "eslint.config.cjs",
        ".eslintrc",
        ".eslintrc.json",
        ".eslintrc.cjs",
        ".eslintrc.js",
        ".eslintrc.yaml",
        ".eslintrc.yml",
    ]
    return any((repo_path / name).is_file() for name in config_names)


def _default_eslint_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "templates" / ".eslintrc.cjs"


def _create_temp_eslint_config(repo_path: Path) -> Path:
    temp_config = repo_path / ".eslintrc.cjs"
    if temp_config.exists():
        return temp_config

    default_config = _default_eslint_config_path()
    temp_config.write_text(default_config.read_text(encoding="utf-8"), encoding="utf-8")
    return temp_config


def run_semgrep(repo_path: Path, output_path: Path) -> Path:
    command = _resolve_command("semgrep", "semgrep") + ["--json", "--config", "auto", "."]
    return _run_command(command, repo_path, output_path)


def _should_retry_with_default_eslint_config(message: str) -> bool:
    message = message.lower()
    return any(
        keyword in message
        for keyword in [
            "eslint couldn't find an eslint.config",
            "root key",
            "extends key",
            "flat config system",
            "cannot read config",
        ]
    )


def run_eslint(repo_path: Path, output_path: Path) -> Path:
    eslint_path = shutil.which("eslint")
    npx_path = shutil.which("npx")

    if eslint_path:
        base_command = [eslint_path]
    elif npx_path:
        base_command = [npx_path, "eslint"]
    else:
        raise FileNotFoundError("명령어를 찾을 수 없습니다: eslint 또는 npx")

    config_exists = _has_eslint_config(repo_path)
    temp_config = None
    try:
        if config_exists:
            command = base_command + ["-f", "json", "."]
            return _run_command(command, repo_path, output_path)

        temp_config = _create_temp_eslint_config(repo_path)
        command = base_command + ["-f", "json", "--config", str(temp_config), "."]
        return _run_command(command, repo_path, output_path)
    except RuntimeError as error:
        if config_exists and _should_retry_with_default_eslint_config(str(error)):
            temp_config = _create_temp_eslint_config(repo_path)
            command = base_command + ["-f", "json", "--config", str(temp_config), "."]
            return _run_command(command, repo_path, output_path)
        raise
    finally:
        if temp_config and temp_config.exists():
            try:
                temp_config.unlink()
            except OSError:
                pass


def run_bandit(repo_path: Path, output_path: Path) -> Path:
    command = _resolve_command("bandit", "bandit") + ["-f", "json", "-r", "."]
    return _run_command(command, repo_path, output_path)


def _start_background_service(command: str, cwd: Path, env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=True,
    )


def _drain_stream(stream, buffer: list[str]) -> None:
    try:
        for line in iter(stream.readline, ""):
            if not line:
                break
            cleaned = line.rstrip()
            if cleaned:
                buffer.append(cleaned)
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _start_output_watchers(process: subprocess.Popen) -> tuple[list[str], list[str]]:
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    if process.stdout is not None:
        threading.Thread(target=_drain_stream, args=(process.stdout, stdout_lines), daemon=True).start()
    if process.stderr is not None:
        threading.Thread(target=_drain_stream, args=(process.stderr, stderr_lines), daemon=True).start()
    return stdout_lines, stderr_lines


def _extract_startup_error(stdout_lines: list[str], stderr_lines: list[str]) -> str | None:
    combined = stderr_lines + stdout_lines
    if not combined:
        return None

    for line in reversed(combined[-200:]):
        lowered = line.lower()
        match = re.search(r"found\s+(\d+)\s+errors?", lowered)
        if match and int(match.group(1)) > 0:
            return line
        if " error " in lowered or "error ts" in lowered:
            return line
        if "is not recognized as an internal or external command" in lowered:
            return line
        if "cannot find module" in lowered:
            return line
    return None


def _detect_package_manager(repo_path: Path) -> list[str] | None:
    if (repo_path / "pnpm-lock.yaml").is_file():
        pnpm_path = shutil.which("pnpm")
        if pnpm_path:
            return [pnpm_path, "install"]
    if (repo_path / "package-lock.json").is_file():
        npm_path = shutil.which("npm")
        if npm_path:
            return [npm_path, "install"]
    if (repo_path / "yarn.lock").is_file():
        yarn_path = shutil.which("yarn")
        if yarn_path:
            return [yarn_path, "install"]
    return None


def _detect_docker_compose_command(repo_path: Path) -> list[str] | None:
    if not (repo_path / "docker-compose.yml").is_file():
        return None

    docker_path = shutil.which("docker")
    if docker_path:
        return [docker_path, "compose"]

    docker_compose_path = shutil.which("docker-compose")
    if docker_compose_path:
        return [docker_compose_path]

    return None


def _looks_like_docker_engine_unavailable(message: str) -> bool:
    lowered = message.lower()
    return any(
        keyword in lowered
        for keyword in [
            "dockerdesktoplinuxengine",
            "error during connect",
            "cannot connect to the docker daemon",
            "docker daemon",
            "the system cannot find the file specified",
        ]
    )


def _find_docker_desktop_executable() -> Path | None:
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Docker" / "Docker" / "Docker Desktop.exe",
        Path(os.environ.get("LocalAppData", "")) / "Programs" / "Docker" / "Docker" / "Docker Desktop.exe",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def _wait_for_docker_engine(base_command: list[str], env: dict[str, str], timeout_seconds: int = 90) -> bool:
    if not base_command:
        return False
    executable = Path(base_command[0]).name.lower()
    if executable not in {"docker", "docker.exe"}:
        return False

    probe_command = [base_command[0], "info"]
    deadline = time.time() + max(timeout_seconds, 10)
    while time.time() < deadline:
        completed = subprocess.run(
            probe_command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        if completed.returncode == 0:
            return True
        time.sleep(3)
    return False


def _detect_prisma_db_push_command(repo_path: Path) -> list[str] | None:
    if not (repo_path / "prisma" / "schema.prisma").is_file():
        return None

    npx_path = shutil.which("npx")
    if npx_path:
        return [npx_path, "prisma", "db", "push"]

    prisma_path = shutil.which("prisma")
    if prisma_path:
        return [prisma_path, "db", "push"]

    return None


def _detect_prisma_seed_command(repo_path: Path) -> list[str] | None:
    if not (repo_path / "prisma" / "seed.ts").is_file():
        return None

    npx_path = shutil.which("npx")
    if npx_path:
        return [npx_path, "prisma", "db", "seed"]

    prisma_path = shutil.which("prisma")
    if prisma_path:
        return [prisma_path, "db", "seed"]

    return None


def _detect_prisma_generate_command(repo_path: Path) -> list[str] | None:
    schema_path = repo_path / "prisma" / "schema.prisma"
    if not schema_path.is_file():
        return None

    npx_path = shutil.which("npx")
    if npx_path:
        return [npx_path, "prisma", "generate", "--schema", str(schema_path)]

    prisma_path = shutil.which("prisma")
    if prisma_path:
        return [prisma_path, "generate", "--schema", str(schema_path)]

    return None


def _should_generate_prisma_client(repo_path: Path) -> bool:
    schema_path = repo_path / "prisma" / "schema.prisma"
    if not schema_path.is_file():
        return False

    # This analyzer starts TypeScript/Nest servers in many different repo states.
    # If the generated Prisma client is stale, the app fails before opening the port,
    # which then looks like a health check problem. Regenerating eagerly is slower,
    # but far more reliable than trying to infer freshness from node_modules layout.
    return True


def _generate_prisma_client(repo_path: Path, env: dict[str, str]) -> None:
    command = _detect_prisma_generate_command(repo_path)
    if command is None:
        raise RuntimeError("Prisma generate를 위한 npx 또는 prisma 명령을 찾을 수 없습니다.")

    print(f"    · Prisma Client 생성 시작: {' '.join(command)}")
    completed = subprocess.run(
        command,
        cwd=repo_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "Prisma Client 생성에 실패했습니다."
        raise RuntimeError(f"Prisma Client 생성 실패: {message}")
    print("    · Prisma Client 생성 완료")


def _should_auto_install_dependencies(repo_path: Path, start_cwd: Path) -> bool:
    package_manager_command = _detect_package_manager(repo_path)
    if package_manager_command is None:
        return False

    root_node_modules = repo_path / "node_modules"
    cwd_node_modules = start_cwd / "node_modules"
    if root_node_modules.exists() or cwd_node_modules.exists():
        return False

    return (repo_path / "package.json").is_file() or (start_cwd / "package.json").is_file()


def _looks_like_missing_runtime_dependency(message: str) -> bool:
    lowered = (message or "").lower()
    patterns = [
        "is not recognized as an internal or external command",
        "cannot find module",
        "command not found",
        "missing script",
        "node_modules",
    ]
    return any(pattern in lowered for pattern in patterns)


def _looks_like_prisma_client_mismatch(message: str) -> bool:
    lowered = (message or "").lower()
    if '@prisma/client' in lowered and 'has no exported member' in lowered:
        return True
    if 'prismaservice' in lowered and 'does not exist on type' in lowered:
        return True
    return False


def _looks_like_missing_local_infra(message: str) -> bool:
    lowered = (message or "").lower()
    if "econnrefused" in lowered and ("ioredis" in lowered or "redis" in lowered):
        return True
    if "econnrefused" in lowered and ("5432" in lowered or "postgres" in lowered or "database" in lowered):
        return True
    if "can't reach database server" in lowered:
        return True
    return False


def _get_managed_newman_binary(repo_path: Path) -> Path:
    tools_dir = repo_path / ".dev-analyzer-tools"
    if os.name == "nt":
        return tools_dir / "node_modules" / ".bin" / "newman.cmd"
    return tools_dir / "node_modules" / ".bin" / "newman"


def _looks_like_broken_newman_runtime(message: str) -> bool:
    lowered = (message or "").lower()
    if "node_modules\\colors\\lib\\colors.js" in lowered and "cannot find module './styles'" in lowered:
        return True
    if "newman\\bin\\newman.js" in lowered and "cannot find module" in lowered:
        return True
    return False


def _resolve_newman_command_v2(repo_path: Path, newman_config: dict, report_path: Path, env: dict[str, str]) -> list[str]:
    collection = newman_config.get("collection")
    if not collection:
        raise RuntimeError("api_test.newman.collection 설정이 필요합니다.")

    managed_newman_path = _get_managed_newman_binary(repo_path)
    if managed_newman_path.is_file():
        command = [str(managed_newman_path), "run", str(collection)]
    else:
        newman_path = shutil.which("newman")
        npx_path = shutil.which("npx")
        if newman_path:
            command = [newman_path, "run", str(collection)]
        elif npx_path:
            command = [npx_path, "--yes", "newman", "run", str(collection)]
        else:
            managed_newman_path = _install_managed_newman(repo_path, env)
            command = [str(managed_newman_path), "run", str(collection)]

    environment = newman_config.get("environment")
    if environment:
        command.extend(["-e", str(environment)])

    reporters = list(newman_config.get("reporters") or ["json"])
    if "cli" not in reporters:
        reporters.insert(0, "cli")
    command.extend(["-r", ",".join(str(reporter) for reporter in reporters)])
    if "json" in reporters:
        command.extend(["--reporter-json-export", str(report_path)])
    return command


def _resolve_newman_command(newman_config: dict, report_path: Path) -> list[str]:
    collection = newman_config.get("collection")
    if not collection:
        raise RuntimeError("api_test.newman.collection 설정이 필요합니다.")

    newman_path = shutil.which("newman")
    npx_path = shutil.which("npx")
    if newman_path:
        command = [newman_path, "run", str(collection)]
    elif npx_path:
        command = [npx_path, "--yes", "newman", "run", str(collection)]
    else:
        raise FileNotFoundError("명령어를 찾을 수 없습니다: newman 또는 npx")

    environment = newman_config.get("environment")
    if environment:
        command.extend(["-e", str(environment)])

    reporters = list(newman_config.get("reporters") or ["json"])
    if "cli" not in reporters:
        reporters.insert(0, "cli")
    command.extend(["-r", ",".join(str(reporter) for reporter in reporters)])
    if "json" in reporters:
        command.extend(["--reporter-json-export", str(report_path)])
    return command


def _count_collection_items(node: dict) -> int:
    items = node.get("item")
    if not isinstance(items, list):
        return 0

    count = 0
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("request"), dict):
            count += 1
        elif isinstance(item, dict):
            count += _count_collection_items(item)
    return count


def _get_newman_total_requests(collection_path: Path) -> int:
    try:
        payload = json.loads(collection_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    return _count_collection_items(payload)


def run_api_tests_latest(repo_path: Path, output_path: Path, force_refresh_design: bool = False) -> Path:
    config = _load_repo_config(repo_path)
    api_test = config.get("api_test")
    if not isinstance(api_test, dict) or not api_test.get("enabled", True):
        raise RuntimeError("API test config is missing or disabled.")

    runner = str(api_test.get("runner", "newman")).strip().lower()
    if runner != "newman":
        raise RuntimeError(f"Unsupported API test runner: {runner}")

    start_command = str(api_test.get("start_command", "")).strip()
    base_url = str(api_test.get("base_url", "")).strip()
    if not start_command:
        raise RuntimeError("api_test.start_command is required.")
    if not base_url:
        raise RuntimeError("api_test.base_url is required.")

    start_cwd = _resolve_repo_relative_path(repo_path, api_test.get("start_cwd"), ".")
    api_test = dict(api_test)
    api_test["__repo_path"] = str(repo_path)
    api_test_env = _build_api_test_runtime_env(api_test)
    docker_services = _get_api_test_docker_services(api_test)
    docker_cleanup_mode = _get_api_test_docker_cleanup_mode(api_test)
    env = _build_process_env(api_test_env)

    healthcheck = api_test.get("healthcheck") if isinstance(api_test.get("healthcheck"), dict) else {}
    healthcheck_path = str(healthcheck.get("path", "/")).strip()
    timeout_seconds = int(healthcheck.get("timeout_seconds", 60))
    interval_seconds = int(healthcheck.get("interval_seconds", 2))

    newman_config = dict(api_test.get("newman") or {})
    collection_path = Path()
    collection_generation: dict[str, object] = {}
    runtime_env_path = None
    runtime_env_original = None
    authorization_matrix_path = None
    authorization_matrix = {}

    _ensure_generated_files_excluded_from_git(repo_path)
    if _should_auto_install_dependencies(repo_path, start_cwd):
        _install_project_dependencies(repo_path, env)
    _initialize_test_database(repo_path, env, api_test, docker_services)
    newman_config, collection_path, collection_generation = _prepare_newman_collection_artifacts(
        repo_path,
        api_test,
        newman_config,
        force_refresh=force_refresh_design,
    )
    authorization_matrix_path, authorization_matrix = _write_authorization_matrix(repo_path, collection_path)
    if _should_generate_prisma_client(repo_path):
        _generate_prisma_client(repo_path, env)
    runtime_env_path, runtime_env_original = _prepare_runtime_env_file(start_cwd, api_test_env)

    report_dir = output_path.parent.resolve()
    report_path = report_dir / "newman_report.json"
    fallback_report_path = (repo_path / output_path.parent / "newman_report.json").resolve()
    for stale_path in [report_path, fallback_report_path]:
        try:
            if stale_path.is_file():
                stale_path.unlink()
        except OSError:
            pass

    print(f"    · API server start: {start_command} (cwd={start_cwd})")
    service = _start_background_service(start_command, start_cwd, env)
    stdout_lines, stderr_lines = _start_output_watchers(service)

    try:
        startup_error = None
        try:
            _wait_for_healthcheck_stable(service, stdout_lines, stderr_lines, base_url, healthcheck_path, timeout_seconds, interval_seconds)
        except RuntimeError as error:
            if _looks_like_prisma_client_mismatch(str(error)):
                _generate_prisma_client(repo_path, env)
                print(f"    · API server restart: {start_command} (cwd={start_cwd})")
                service = _start_background_service(start_command, start_cwd, env)
                stdout_lines, stderr_lines = _start_output_watchers(service)
                _wait_for_healthcheck_stable(service, stdout_lines, stderr_lines, base_url, healthcheck_path, timeout_seconds, interval_seconds)
            elif _looks_like_missing_local_infra(str(error)):
                _start_local_infra_services_with_autostart(repo_path, env, docker_services)
                print(f"    · API server restart: {start_command} (cwd={start_cwd})")
                service = _start_background_service(start_command, start_cwd, env)
                stdout_lines, stderr_lines = _start_output_watchers(service)
                _wait_for_healthcheck_stable(service, stdout_lines, stderr_lines, base_url, healthcheck_path, timeout_seconds, interval_seconds)
            else:
                startup_error = error

        if startup_error is not None:
            if not _looks_like_missing_runtime_dependency(str(startup_error)):
                raise startup_error

            _install_project_dependencies(repo_path, env)
            if _should_generate_prisma_client(repo_path):
                _generate_prisma_client(repo_path, env)
            print(f"    · API server restart: {start_command} (cwd={start_cwd})")
            service = _start_background_service(start_command, start_cwd, env)
            stdout_lines, stderr_lines = _start_output_watchers(service)
            _wait_for_healthcheck_stable(service, stdout_lines, stderr_lines, base_url, healthcheck_path, timeout_seconds, interval_seconds)

        command = _resolve_newman_command_v2(repo_path, newman_config, report_path, env)
        total_requests = _get_newman_total_requests(collection_path)
        total_label = total_requests if total_requests > 0 else "?"
        print(f"    · Newman target API count: {total_label}")
        print(f"    · Newman start: {' '.join(command)}")
        completed = _run_newman_with_progress(command, repo_path, env, total_requests)
        if completed.returncode != 0 and _looks_like_broken_newman_runtime(completed.stdout):
            managed_newman_path = _install_managed_newman(repo_path, env)
            command = _resolve_newman_command_v2(repo_path, newman_config, report_path, env)
            print(f"    · Newman runtime recovered, retry: {managed_newman_path}")
            completed = _run_newman_with_progress(command, repo_path, env, total_requests)

        effective_report_path = report_path
        if not effective_report_path.is_file() and fallback_report_path.is_file():
            effective_report_path = fallback_report_path
        if not effective_report_path.is_file():
            raise RuntimeError("API test result file was not created.")
        print("    · Newman completed")

        output = {
            "runner": runner,
            "base_url": base_url,
            "healthcheck_path": healthcheck_path,
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "report": json.loads(effective_report_path.read_text(encoding="utf-8") or "{}"),
            "collection_generation": collection_generation,
            "authorization_matrix_path": str(authorization_matrix_path) if authorization_matrix_path else "",
            "authorization_matrix": authorization_matrix,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or "Newman execution failed."
            raise RuntimeError(message)
        return output_path
    finally:
        try:
            if service.poll() is None:
                service.terminate()
                service.wait(timeout=10)
        except subprocess.TimeoutExpired:
            service.kill()
        except OSError:
            pass
        if docker_cleanup_mode != "keep":
            _cleanup_local_infra_services(repo_path, env, docker_services, docker_cleanup_mode)
        _restore_runtime_env_file(runtime_env_path, runtime_env_original)


def _normalize_seed_model_name(name: str) -> str:
    text = re.sub(r"^[\d\-_ ]+", "", str(name or "").strip())
    if not text:
        return ""
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", text) if part]
    if not parts:
        return ""
    first = parts[0]
    if len(parts) == 1:
        return first[:1].lower() + first[1:]
    return first[:1].lower() + first[1:] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def _coerce_seed_scalar(value: object) -> object:
    if value is None:
        return None
    if not isinstance(value, str):
        return value

    text = value.strip()
    if text == "":
        return None
    lowered = text.lower()
    if lowered == "null":
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"-?\d+", text):
        try:
            return int(text)
        except ValueError:
            return text
    if re.fullmatch(r"-?\d+\.\d+", text):
        try:
            return float(text)
        except ValueError:
            return text
    if (text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]")):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text


def _normalize_seed_rows(rows: object) -> list[dict]:
    normalized: list[dict] = []
    if not isinstance(rows, list):
        return normalized
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized.append({str(key): _coerce_seed_scalar(value) for key, value in row.items()})
    return normalized


def _load_seed_json_file(path: Path) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    jobs: list[dict] = []
    if isinstance(payload, dict) and isinstance(payload.get("entries"), list):
        for entry in payload.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            model = _normalize_seed_model_name(str(entry.get("model") or ""))
            rows = _normalize_seed_rows(entry.get("rows"))
            if model and rows:
                jobs.append({"model": model, "rows": rows, "source": str(path.name)})
        return jobs

    if isinstance(payload, dict):
        for model_name, rows in payload.items():
            model = _normalize_seed_model_name(str(model_name))
            normalized_rows = _normalize_seed_rows(rows)
            if model and normalized_rows:
                jobs.append({"model": model, "rows": normalized_rows, "source": str(path.name)})
        return jobs

    if isinstance(payload, list):
        model = _normalize_seed_model_name(path.stem)
        rows = _normalize_seed_rows(payload)
        if model and rows:
            jobs.append({"model": model, "rows": rows, "source": str(path.name)})
    return jobs


def _load_seed_csv_file(path: Path) -> list[dict]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = [{str(key): _coerce_seed_scalar(value) for key, value in row.items()} for row in reader]
    except OSError:
        return []

    model = _normalize_seed_model_name(path.stem)
    normalized_rows = _normalize_seed_rows(rows)
    if not model or not normalized_rows:
        return []
    return [{"model": model, "rows": normalized_rows, "source": str(path.name)}]


def _seed_files_exist(repo_path: Path) -> bool:
    if (repo_path / ".dev-analyzer.seed.json").is_file():
        return True
    seed_dir = repo_path / ".dev-analyzer.seed"
    if not seed_dir.is_dir():
        return False
    return any(path.is_file() and path.suffix.lower() in {".json", ".csv"} for path in seed_dir.iterdir())


def _parse_prisma_enum_values(schema_text: str) -> dict[str, list[str]]:
    enum_map: dict[str, list[str]] = {}
    for match in re.finditer(r"enum\s+(\w+)\s*\{(.*?)\}", schema_text, re.DOTALL):
        enum_name = match.group(1)
        body = match.group(2)
        values: list[str] = []
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("//") or line.startswith("@@"):
                continue
            token = re.split(r"\s+", line, maxsplit=1)[0]
            if token:
                values.append(token)
        if values:
            enum_map[enum_name] = values
    return enum_map


def _parse_prisma_model_blocks(schema_text: str) -> list[tuple[str, str]]:
    return [(match.group(1), match.group(2)) for match in re.finditer(r"model\s+(\w+)\s*\{(.*?)\}", schema_text, re.DOTALL)]


def _infer_seed_value(model_name: str, field_name: str, field_type: str, enum_map: dict[str, list[str]]) -> object | None:
    lowered = field_name.lower()
    if field_type in enum_map:
        values = enum_map.get(field_type) or []
        return values[0] if values else None
    if field_type == "String":
        if "email" in lowered:
            return f"{model_name.lower()}.{field_name.lower()}@example.com"
        if "phone" in lowered:
            return "010-0000-0000"
        if lowered == "code" or lowered.endswith("code"):
            return f"{model_name.upper()}_001"
        if lowered == "name" or lowered.endswith("name"):
            return f"Sample {model_name}"
        if "title" in lowered:
            return f"Sample {model_name} Title"
        if "description" in lowered or "summary" in lowered or "content" in lowered:
            return f"Sample {model_name} {field_name}"
        if "url" in lowered:
            return f"https://example.com/{model_name.lower()}"
        return f"sample_{model_name.lower()}_{field_name.lower()}"
    if field_type in {"Int", "BigInt"}:
        return 1
    if field_type in {"Float", "Decimal"}:
        return 1
    if field_type == "Boolean":
        return True
    if field_type == "DateTime":
        return "2026-01-01T00:00:00.000Z"
    if field_type == "Json":
        return {}
    return None


def _build_seed_rows_from_schema(repo_path: Path) -> dict[str, list[dict]]:
    schema_path = repo_path / "prisma" / "schema.prisma"
    if not schema_path.is_file():
        return {}

    try:
        schema_text = schema_path.read_text(encoding="utf-8")
    except OSError:
        return {}

    enum_map = _parse_prisma_enum_values(schema_text)
    model_names = {name for name, _ in _parse_prisma_model_blocks(schema_text)}
    seed_data: dict[str, list[dict]] = {}

    for model_name, body in _parse_prisma_model_blocks(schema_text):
        row: dict[str, object] = {}
        skip_model = False

        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("//") or line.startswith("@@"):
                continue

            parts = re.split(r"\s+", line)
            if len(parts) < 2:
                continue

            field_name = parts[0]
            field_type_token = parts[1]
            field_type = field_type_token.rstrip("?")
            is_optional = field_type_token.endswith("?")
            is_list = field_type_token.endswith("[]")
            if is_list:
                continue

            annotations = " ".join(parts[2:])
            if "@relation" in annotations:
                skip_model = True
                break
            if field_type in model_names and not is_optional:
                skip_model = True
                break
            if field_name in {"id", "createdAt", "updatedAt", "deletedAt"}:
                continue
            if "@default(" in annotations or "@updatedAt" in annotations:
                continue
            if is_optional:
                continue
            if field_type == "Bytes":
                skip_model = True
                break

            inferred_value = _infer_seed_value(model_name, field_name, field_type, enum_map)
            if inferred_value is None:
                skip_model = True
                break
            row[field_name] = inferred_value

        if skip_model or not row:
            continue

        accessor = model_name[:1].lower() + model_name[1:]
        seed_data[accessor] = [row]

    return seed_data


def _write_auto_seed_script(script_path: Path) -> None:
    script = """const fs = require('fs');
const { PrismaClient } = require('@prisma/client');
const { PrismaPg } = require('@prisma/adapter-pg');

async function main() {
  const payloadPath = process.argv[2];
  if (!payloadPath) {
    throw new Error('Missing seed payload path');
  }

  const connectionString = process.env.DATABASE_URL;
  if (!connectionString) {
    throw new Error('DATABASE_URL is not set');
  }

  const payload = JSON.parse(fs.readFileSync(payloadPath, 'utf8'));
  const adapter = new PrismaPg({ connectionString });
  const prisma = new PrismaClient({ adapter });

  try {
    for (const job of payload.jobs || []) {
      const model = job.model;
      const rows = Array.isArray(job.rows) ? job.rows : [];
      const delegate = prisma[model];
      if (!delegate || typeof delegate.count !== 'function' || typeof delegate.create !== 'function') {
        console.log(`[dev-analyzer-seed] skip ${model}: Prisma model accessor not found`);
        continue;
      }

      const existingCount = await delegate.count();
      if (existingCount > 0) {
        console.log(`[dev-analyzer-seed] skip ${model}: existing rows=${existingCount}`);
        continue;
      }

      for (const row of rows) {
        await delegate.create({ data: row });
      }
      console.log(`[dev-analyzer-seed] seeded ${model}: inserted=${rows.length}`);
    }
  } finally {
    await prisma.$disconnect();
  }
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
"""
    script_path.write_text(script, encoding="utf-8")


def _apply_external_seed_data_if_needed(repo_path: Path, env: dict[str, str]) -> None:
    jobs = _load_external_seed_jobs(repo_path)
    if not jobs:
        return

    node_path = shutil.which("node")
    if not node_path:
        raise RuntimeError("외부 seed 데이터 적용을 위한 node 명령을 찾을 수 없습니다.")

    tools_dir = repo_path / ".dev-analyzer-tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    payload_path = tools_dir / "auto-seed-payload.json"
    script_path = tools_dir / "auto-seed-runner.cjs"
    payload_path.write_text(json.dumps({"jobs": jobs}, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_auto_seed_script(script_path)

    command = [node_path, str(script_path), str(payload_path)]
    print(f"    · 외부 seed 데이터 확인 시작: {' '.join(command)}")
    completed = subprocess.run(
        command,
        cwd=repo_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if completed.stdout.strip():
        for line in completed.stdout.splitlines():
            cleaned = line.strip()
            if cleaned:
                print(f"    · {cleaned}")
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "외부 seed 데이터 적용에 실패했습니다."
        raise RuntimeError(f"외부 seed 데이터 적용 실패: {message}")
    print("    · 외부 seed 데이터 확인 완료")


def _iter_sql_schema_files(repo_path: Path) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()
    patterns = [
        "schema.sql",
        "*.sql",
    ]
    skip_parts = {"node_modules", ".git", "dist", "build", "output", ".next"}

    for pattern in patterns:
        for path in repo_path.rglob(pattern):
            if not path.is_file():
                continue
            if any(part in skip_parts for part in path.parts):
                continue
            lower = str(path).lower()
            if pattern == "*.sql" and not any(token in lower for token in ["schema", "migration", "migrations", "sql", "dump"]):
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            candidates.append(resolved)
    return sorted(candidates, key=lambda item: (len(item.parts), str(item)))


def _infer_seed_value_from_sql(field_name: str, sql_type: str) -> object | None:
    lowered_name = field_name.lower()
    lowered_type = sql_type.lower()

    if any(token in lowered_type for token in ["char", "text", "json", "uuid"]):
        if "email" in lowered_name:
            return f"{lowered_name}@example.com"
        if "phone" in lowered_name or "mobile" in lowered_name:
            return "010-0000-0000"
        if lowered_name == "code" or lowered_name.endswith("_code"):
            return "SAMPLE_001"
        if lowered_name == "name" or lowered_name.endswith("_name"):
            return "Sample Name"
        if "title" in lowered_name:
            return "Sample Title"
        if "url" in lowered_name:
            return "https://example.com/sample"
        if "description" in lowered_name or "summary" in lowered_name or "content" in lowered_name:
            return f"Sample {field_name}"
        return f"sample_{lowered_name}"
    if any(token in lowered_type for token in ["bool"]):
        return True
    if any(token in lowered_type for token in ["int", "numeric", "decimal", "float", "double", "real"]):
        return 1
    if any(token in lowered_type for token in ["date", "time"]):
        return "2026-01-01T00:00:00.000Z"
    return None


def _build_seed_rows_from_sql(repo_path: Path) -> dict[str, list[dict]]:
    table_rows: dict[str, list[dict]] = {}
    create_table_pattern = re.compile(
        r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:[\w\"]+\.)?\"?([\w]+)\"?\s*\((.*?)\);",
        re.IGNORECASE | re.DOTALL,
    )

    for path in _iter_sql_schema_files(repo_path):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for table_name, body in create_table_pattern.findall(text):
            accessor = _normalize_seed_model_name(table_name)
            if not accessor or accessor in table_rows:
                continue

            row: dict[str, object] = {}
            skip_table = False
            for raw_line in body.splitlines():
                line = raw_line.strip().rstrip(",")
                if not line:
                    continue
                lowered = line.lower()
                if lowered.startswith(("constraint", "primary key", "foreign key", "unique", "key ", "index ", "check ")):
                    continue

                match = re.match(r"\"?([\w]+)\"?\s+([A-Za-z0-9_()]+)", line)
                if not match:
                    continue
                column_name = match.group(1)
                sql_type = match.group(2)

                if column_name.lower() in {"id", "created_at", "updated_at", "deleted_at"}:
                    continue
                if any(token in lowered for token in ["serial", "identity", "auto_increment", "references", "default now()", "default current_timestamp"]):
                    continue

                inferred = _infer_seed_value_from_sql(column_name, sql_type)
                if inferred is None:
                    skip_table = True
                    break
                row[column_name] = inferred

            if not skip_table and row:
                table_rows[accessor] = [row]

        if table_rows:
            return table_rows
    return table_rows


def _infer_seed_value_from_source(field_name: str, field_type: str) -> object | None:
    lowered_name = field_name.lower()
    lowered_type = field_type.lower()
    if "string" in lowered_type:
        if "email" in lowered_name:
            return f"{lowered_name}@example.com"
        if "phone" in lowered_name:
            return "010-0000-0000"
        if lowered_name == "code" or lowered_name.endswith("code"):
            return "SAMPLE_001"
        if "name" in lowered_name:
            return "Sample Name"
        if "title" in lowered_name:
            return "Sample Title"
        return f"sample_{lowered_name}"
    if any(token in lowered_type for token in ["number", "int", "bigint", "float", "decimal"]):
        return 1
    if "boolean" in lowered_type or lowered_type == "bool":
        return True
    if "date" in lowered_type:
        return "2026-01-01T00:00:00.000Z"
    return None


def _build_seed_rows_from_entity_source(repo_path: Path) -> dict[str, list[dict]]:
    candidates = sorted(repo_path.rglob("*.entity.ts"), key=lambda item: (len(item.parts), str(item)))
    results: dict[str, list[dict]] = {}
    skip_parts = {"node_modules", ".git", "dist", "build", "output", ".next"}

    for path in candidates:
        if any(part in skip_parts for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        class_match = re.search(r"export\s+class\s+(\w+)", text)
        if not class_match:
            continue

        model_name = _normalize_seed_model_name(class_match.group(1))
        if not model_name or model_name in results:
            continue

        row: dict[str, object] = {}
        pending_column = False
        skip_model = False
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if any(line.startswith(prefix) for prefix in ["@ManyToOne", "@OneToMany", "@OneToOne", "@ManyToMany"]):
                skip_model = True
                break
            if line.startswith("@Column"):
                pending_column = True
                continue
            if not pending_column:
                continue
            match = re.match(r"(?:public|private|protected)?\s*(\w+)\??:\s*([\w\[\]]+)", line)
            pending_column = False
            if not match:
                continue
            field_name = match.group(1)
            field_type = match.group(2)
            if field_name.lower() in {"id", "createdat", "updatedat", "deletedat"}:
                continue
            inferred = _infer_seed_value_from_source(field_name, field_type)
            if inferred is None:
                skip_model = True
                break
            row[field_name] = inferred

        if not skip_model and row:
            results[model_name] = [row]
    return results


def _build_seed_rows_from_source(repo_path: Path) -> dict[str, list[dict]]:
    entity_rows = _build_seed_rows_from_entity_source(repo_path)
    if entity_rows:
        return entity_rows

    candidates = sorted(
        list(repo_path.rglob("*.controller.ts")) + list(repo_path.rglob("*.service.ts")),
        key=lambda item: (len(item.parts), str(item)),
    )
    skip_parts = {"node_modules", ".git", "dist", "build", "output", ".next"}
    results: dict[str, list[dict]] = {}

    for path in candidates:
        if any(part in skip_parts for part in path.parts):
            continue
        stem = path.stem.replace(".controller", "").replace(".service", "")
        model_name = _normalize_seed_model_name(stem)
        if not model_name or model_name in {"health", "auth", "mail", "jwt", "redis"} or model_name in results:
            continue
        results[model_name] = [
            {
                "code": f"{model_name.upper()}_001",
                "name": f"Sample {model_name[:1].upper() + model_name[1:]}",
                "description": f"Auto-generated seed draft for {model_name}",
            }
        ]
        if len(results) >= 10:
            break
    return results


def _ensure_external_seed_template(repo_path: Path) -> Path | None:
    if _seed_files_exist(repo_path):
        return None

    seed_data = _build_seed_rows_from_schema(repo_path)
    source_label = "Prisma schema"
    if not seed_data:
        seed_data = _build_seed_rows_from_sql(repo_path)
        source_label = "SQL schema"
    if not seed_data:
        seed_data = _build_seed_rows_from_source(repo_path)
        source_label = "source code"
    if not seed_data:
        return None

    target_path = repo_path / ".dev-analyzer.seed.json"
    target_path.write_text(json.dumps(seed_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"    · 외부 seed 템플릿 자동 생성 ({source_label}): {target_path}")
    return target_path


def _load_external_seed_jobs(repo_path: Path) -> list[dict]:
    _ensure_external_seed_template(repo_path)
    jobs: list[dict] = []

    root_json = repo_path / ".dev-analyzer.seed.json"
    if root_json.is_file():
        jobs.extend(_load_seed_json_file(root_json))

    seed_dir = repo_path / ".dev-analyzer.seed"
    if seed_dir.is_dir():
        for path in sorted(seed_dir.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_file():
                continue
            if path.suffix.lower() == ".json":
                jobs.extend(_load_seed_json_file(path))
            elif path.suffix.lower() == ".csv":
                jobs.extend(_load_seed_csv_file(path))

    return jobs


def _start_docker_desktop_if_available(base_command: list[str], env: dict[str, str]) -> bool:
    desktop_path = _find_docker_desktop_executable()
    if desktop_path is None:
        print("    · Docker Desktop 자동 실행 건너뜀: 실행 파일을 찾지 못했습니다.")
        return False

    print(f"    · Docker Desktop 실행 시작: {desktop_path}")
    try:
        subprocess.Popen(
            [str(desktop_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        print(f"    · Docker Desktop 자동 실행 실패: {error}")
        return False

    print("    · Docker 엔진 준비 대기 시작")
    if _wait_for_docker_engine(base_command, env):
        print("    · Docker 엔진 준비 완료")
        return True

    print("    · Docker 엔진 준비 시간 초과")
    return False


def _install_project_dependencies(repo_path: Path, env: dict[str, str]) -> None:
    command = _detect_package_manager(repo_path)
    if command is None:
        raise RuntimeError("의존성 자동 설치를 위한 package manager를 찾을 수 없습니다.")

    print(f"    · 의존성 자동 설치 시작: {' '.join(command)}")
    completed = subprocess.run(
        command,
        cwd=repo_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "의존성 자동 설치에 실패했습니다."
        raise RuntimeError(f"의존성 자동 설치 실패: {message}")
    print("    · 의존성 자동 설치 완료")


def _start_local_infra_services(repo_path: Path, env: dict[str, str], services: list[str]) -> None:
    base_command = _detect_docker_compose_command(repo_path)
    if base_command is None:
        raise RuntimeError("로컬 인프라 자동 기동을 위한 docker compose 명령을 찾을 수 없습니다.")

    command = base_command + ["up", "-d"] + services
    print(f"    · 로컬 인프라 기동 시작: {' '.join(command)}")
    completed = subprocess.run(
        command,
        cwd=repo_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "로컬 인프라 기동에 실패했습니다."
        raise RuntimeError(f"로컬 인프라 기동 실패: {message}")
    print("    · 로컬 인프라 기동 완료")


def _start_local_infra_services_with_autostart(repo_path: Path, env: dict[str, str], services: list[str]) -> None:
    base_command = _detect_docker_compose_command(repo_path)
    if base_command is None:
        raise RuntimeError("로컬 인프라 자동 기동을 위한 docker compose 명령을 찾을 수 없습니다.")

    command = base_command + ["up", "-d"] + services
    print(f"    · 로컬 인프라 기동 시작: {' '.join(command)}")
    completed = subprocess.run(
        command,
        cwd=repo_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "로컬 인프라 기동에 실패했습니다."
        if _looks_like_docker_engine_unavailable(message) and _start_docker_desktop_if_available(base_command, env):
            print(f"    · 로컬 인프라 기동 재시도: {' '.join(command)}")
            completed = subprocess.run(
                command,
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            message = completed.stderr.strip() or completed.stdout.strip() or "로컬 인프라 기동에 실패했습니다."
        if completed.returncode != 0:
            raise RuntimeError(f"로컬 인프라 기동 실패: {message}")
    print("    · 로컬 인프라 기동 완료")


def _cleanup_local_infra_services(repo_path: Path, env: dict[str, str], services: list[str], cleanup_mode: str) -> None:
    if cleanup_mode == "keep":
        return

    base_command = _detect_docker_compose_command(repo_path)
    if base_command is None:
        print("    · 로컬 인프라 정리 건너뜀: docker compose 명령을 찾을 수 없습니다.")
        return

    if cleanup_mode == "stop":
        command = base_command + ["stop"] + services
        label = "정지"
    elif cleanup_mode == "down":
        command = base_command + ["down"]
        label = "종료"
    elif cleanup_mode == "down_volumes":
        command = base_command + ["down", "-v"]
        label = "종료 및 볼륨 제거"
    else:
        return

    print(f"    · 로컬 인프라 {label} 시작: {' '.join(command)}")
    completed = subprocess.run(
        command,
        cwd=repo_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "로컬 인프라 정리에 실패했습니다."
        print(f"    · 로컬 인프라 정리 실패: {message}")
        return
    print(f"    · 로컬 인프라 {label} 완료")


def _run_checked_command(command: list[str], cwd: Path, env: dict[str, str], start_message: str, success_message: str) -> None:
    print(start_message.format(command=" ".join(command)))
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "명령 실행에 실패했습니다."
        raise RuntimeError(message)
    print(success_message)


def _initialize_test_database(repo_path: Path, env: dict[str, str], api_test: dict, docker_services: list[str]) -> bool:
    _ensure_external_seed_template(repo_path)

    database_config = api_test.get("database") if isinstance(api_test.get("database"), dict) else {}
    database_type = str(database_config.get("type") or "").strip().lower()
    if database_type not in {"postgres", "postgresql"}:
        return False

    init_config = _get_database_init_config(api_test)
    if not init_config["enabled"]:
        return False

    push_command = _detect_prisma_db_push_command(repo_path)
    if push_command is None:
        return False

    infra_started = False

    def run_db_push() -> None:
        _run_checked_command(
            push_command,
            repo_path,
            env,
            "    · 테스트 DB 스키마 적용 시작: {command}",
            "    · 테스트 DB 스키마 적용 완료",
        )

    try:
        if init_config["mode"] == "db_push":
            run_db_push()
    except RuntimeError as error:
        if _looks_like_missing_local_infra(str(error)):
            _start_local_infra_services_with_autostart(repo_path, env, docker_services)
            infra_started = True
            time.sleep(5)
            if init_config["mode"] == "db_push":
                run_db_push()
        else:
            raise RuntimeError(f"테스트 DB 초기화 실패: {error}") from error

    _apply_external_seed_data_if_needed(repo_path, env)
    return infra_started


def _wait_for_healthcheck(
    process: subprocess.Popen,
    stdout_lines: list[str],
    stderr_lines: list[str],
    base_url: str,
    path: str,
    timeout_seconds: int,
    interval_seconds: int,
) -> None:
    path = (path or "").strip()
    if not path:
        return

    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    started_at = time.time()
    deadline = started_at + max(timeout_seconds, 1)
    last_error = ""
    last_progress_log_at = 0.0
    last_stdout_index = 0
    last_stderr_index = 0
    print(f"    · health check 대기 시작: {url}")

    while time.time() < deadline:
        now = time.time()
        if len(stdout_lines) > last_stdout_index:
            for line in stdout_lines[last_stdout_index:]:
                print(f"    · 서버 로그(stdout): {line}")
            last_stdout_index = len(stdout_lines)
        if len(stderr_lines) > last_stderr_index:
            for line in stderr_lines[last_stderr_index:]:
                print(f"    · 서버 로그(stderr): {line}")
            last_stderr_index = len(stderr_lines)

        startup_error = _extract_startup_error(stdout_lines, stderr_lines)
        if startup_error:
            raise RuntimeError(f"API 서버 시작 실패: {startup_error}")
        if process.poll() is not None:
            message = "\n".join((stderr_lines + stdout_lines)[-20:]).strip() or "서버 프로세스가 조기 종료되었습니다."
            raise RuntimeError(f"API 서버 실행 실패: {message}")

        try:
            with urllib_request.urlopen(url, timeout=5) as response:
                status = getattr(response, "status", 200)
                if 200 <= status < 500:
                    print(f"    · health check 성공: {url} (status={status})")
                    return
        except urllib_error.HTTPError as error:
            if 200 <= error.code < 500:
                print(f"    · health check 성공: {url} (status={error.code})")
                return
            last_error = str(error)
        except (urllib_error.URLError, TimeoutError, ValueError) as error:
            last_error = str(error)

        if now - last_progress_log_at >= 5:
            elapsed = int(now - started_at)
            remaining = max(int(deadline - now), 0)
            detail = f", 마지막 에러: {last_error}" if last_error else ""
            print(f"    · health check 대기 중... 경과 {elapsed}초, 남은 시간 {remaining}초{detail}")
            last_progress_log_at = now
        time.sleep(max(interval_seconds, 1))

    raise RuntimeError(f"API 서버 health check 대기 시간이 초과되었습니다: {url} ({last_error})")


def _wait_for_healthcheck_stable(
    process: subprocess.Popen,
    stdout_lines: list[str],
    stderr_lines: list[str],
    base_url: str,
    path: str,
    timeout_seconds: int,
    interval_seconds: int,
) -> None:
    path = (path or "").strip()
    if not path:
        return

    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    started_at = time.time()
    deadline = started_at + max(timeout_seconds, 1)
    last_error = ""
    last_progress_log_at = 0.0
    last_stdout_index = 0
    last_stderr_index = 0
    success_streak = 0
    print(f"    · health check 대기 시작: {url}")

    while time.time() < deadline:
        now = time.time()
        if len(stdout_lines) > last_stdout_index:
            for line in stdout_lines[last_stdout_index:]:
                print(f"    · 서버 로그(stdout): {line}")
            last_stdout_index = len(stdout_lines)
        if len(stderr_lines) > last_stderr_index:
            for line in stderr_lines[last_stderr_index:]:
                print(f"    · 서버 로그(stderr): {line}")
            last_stderr_index = len(stderr_lines)

        startup_error = _extract_startup_error(stdout_lines, stderr_lines)
        if startup_error:
            raise RuntimeError(f"API 서버 시작 실패: {startup_error}")
        if process.poll() is not None:
            message = "\n".join((stderr_lines + stdout_lines)[-20:]).strip() or "서버 프로세스가 조기 종료되었습니다."
            raise RuntimeError(f"API 서버 실행 실패: {message}")

        try:
            with urllib_request.urlopen(url, timeout=5) as response:
                status = getattr(response, "status", 200)
                if 200 <= status < 500:
                    success_streak += 1
                    if success_streak == 1:
                        print(f"    · health check 1차 성공: {url} (status={status})")
                    else:
                        print(f"    · health check 성공: {url} (status={status})")
                        return
        except urllib_error.HTTPError as error:
            if 200 <= error.code < 500:
                success_streak += 1
                if success_streak == 1:
                    print(f"    · health check 1차 성공: {url} (status={error.code})")
                else:
                    print(f"    · health check 성공: {url} (status={error.code})")
                    return
            else:
                success_streak = 0
                last_error = str(error)
        except (urllib_error.URLError, TimeoutError, ValueError) as error:
            success_streak = 0
            last_error = str(error)

        if now - last_progress_log_at >= 5:
            elapsed = int(now - started_at)
            remaining = max(int(deadline - now), 0)
            detail = f", 마지막 에러: {last_error}" if last_error else ""
            print(f"    · health check 대기 중... 경과 {elapsed}초, 남은 시간 {remaining}초{detail}")
            last_progress_log_at = now
        time.sleep(max(interval_seconds, 1))

    raise RuntimeError(f"API 서버 health check 대기 시간이 초과되었습니다: {url} ({last_error})")


def _install_managed_newman(repo_path: Path, env: dict[str, str]) -> Path:
    npm_path = shutil.which("npm")
    if not npm_path:
        raise FileNotFoundError("명령어를 찾을 수 없습니다: npm")

    tools_dir = repo_path / ".dev-analyzer-tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    command = [npm_path, "install", "--no-save", "--prefix", str(tools_dir), "newman"]
    print(f"    · Newman 자동 설치 시작: {' '.join(command)}")
    completed = subprocess.run(
        command,
        cwd=repo_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "Newman 자동 설치에 실패했습니다."
        raise RuntimeError(f"Newman 자동 설치 실패: {message}")

    binary_path = _get_managed_newman_binary(repo_path)
    if not binary_path.is_file():
        raise RuntimeError("Newman 자동 설치 후 실행 파일을 찾을 수 없습니다.")

    print("    · Newman 자동 설치 완료")
    return binary_path


def _run_newman_with_progress(command: list[str], cwd: Path, env: dict[str, str], total_requests: int) -> subprocess.CompletedProcess:
    completed_requests = 0
    output_lines: list[str] = []
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    start_time = time.time()
    last_progress_time = start_time

    if process.stdout is not None:
        for raw_line in iter(process.stdout.readline, ""):
            if not raw_line:
                break
            line = raw_line.rstrip()
            output_lines.append(line)
            normalized = line.strip()
            now = time.time()
            if normalized.startswith("→"):
                completed_requests += 1
                total_label = total_requests if total_requests > 0 else "?"
                print(f"    · 현재 테스트 완료 API 수 / 전체 API 수: {completed_requests} / {total_label}")
                last_progress_time = now
            elif normalized and now - last_progress_time >= 10:
                elapsed = int(now - start_time)
                total_label = total_requests if total_requests > 0 else "?"
                print(f"    · Newman 실행 중... 경과 {elapsed}초, 현재 테스트 완료 API 수 / 전체 API 수: {completed_requests} / {total_label}")
                last_progress_time = now

    returncode = process.wait()
    stdout = "\n".join(output_lines)
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")


def run_api_tests(repo_path: Path, output_path: Path, force_refresh_design: bool = False) -> Path:
    return run_api_tests_latest(repo_path, output_path, force_refresh_design=force_refresh_design)
