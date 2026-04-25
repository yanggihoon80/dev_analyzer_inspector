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


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO_CONFIG_TEMPLATE = PROJECT_ROOT / ".dev-analyzer.example.yml"


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
    collection_candidates = sorted(repo_path.rglob("*.collection.json"), key=lambda item: (len(item.parts), str(item)))
    environment_candidates = sorted(
        repo_path.rglob("*environment*.json"),
        key=lambda item: (len(item.parts), str(item)),
    )

    config: dict[str, object] = {
        "collection": "tests/postman/collection.json",
        "reporters": ["json"],
    }
    if collection_candidates:
        config["collection"] = collection_candidates[0].relative_to(repo_path).as_posix()
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


def run_api_tests_latest(repo_path: Path, output_path: Path) -> Path:
    config = _load_repo_config(repo_path)
    api_test = config.get("api_test")
    if not isinstance(api_test, dict) or not api_test.get("enabled", True):
        raise RuntimeError("API 테스트 설정이 없거나 비활성화되어 있습니다.")

    runner = str(api_test.get("runner", "newman")).strip().lower()
    if runner != "newman":
        raise RuntimeError(f"지원하지 않는 API 테스트 runner입니다: {runner}")

    start_command = str(api_test.get("start_command", "")).strip()
    base_url = str(api_test.get("base_url", "")).strip()
    if not start_command:
        raise RuntimeError("api_test.start_command 설정이 필요합니다.")
    if not base_url:
        raise RuntimeError("api_test.base_url 설정이 필요합니다.")

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
    for key in ["collection", "environment"]:
        if newman_config.get(key):
            newman_config[key] = str(_resolve_repo_relative_path(repo_path, str(newman_config[key])))
    collection_path = Path(str(newman_config.get("collection", "")))
    environment_path_text = str(newman_config.get("environment") or "").strip()
    if environment_path_text and not Path(environment_path_text).is_file():
        print(f"    · Newman environment 파일을 찾지 못해 옵션을 건너뜁니다: {environment_path_text}")
        newman_config.pop("environment", None)

    runtime_env_path = None
    runtime_env_original = None
    authorization_matrix_path = None
    authorization_matrix = {}

    if _should_auto_install_dependencies(repo_path, start_cwd):
        _install_project_dependencies(repo_path, env)
    _initialize_test_database(repo_path, env, api_test, docker_services)
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

    print(f"    · API 서버 실행 시작: {start_command} (cwd={start_cwd})")
    service = _start_background_service(start_command, start_cwd, env)
    stdout_lines, stderr_lines = _start_output_watchers(service)

    try:
        startup_error = None
        try:
            _wait_for_healthcheck_stable(service, stdout_lines, stderr_lines, base_url, healthcheck_path, timeout_seconds, interval_seconds)
        except RuntimeError as error:
            if _looks_like_prisma_client_mismatch(str(error)):
                _generate_prisma_client(repo_path, env)
                print(f"    · API 서버 재시작: {start_command} (cwd={start_cwd})")
                service = _start_background_service(start_command, start_cwd, env)
                stdout_lines, stderr_lines = _start_output_watchers(service)
                _wait_for_healthcheck_stable(service, stdout_lines, stderr_lines, base_url, healthcheck_path, timeout_seconds, interval_seconds)
            elif _looks_like_missing_local_infra(str(error)):
                _start_local_infra_services_with_autostart(repo_path, env, docker_services)
                print(f"    · API 서버 재시작: {start_command} (cwd={start_cwd})")
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
            print(f"    · API 서버 재시작: {start_command} (cwd={start_cwd})")
            service = _start_background_service(start_command, start_cwd, env)
            stdout_lines, stderr_lines = _start_output_watchers(service)
            _wait_for_healthcheck_stable(service, stdout_lines, stderr_lines, base_url, healthcheck_path, timeout_seconds, interval_seconds)

        command = _resolve_newman_command_v2(repo_path, newman_config, report_path, env)
        total_requests = _get_newman_total_requests(collection_path)
        total_label = total_requests if total_requests > 0 else "?"
        print(f"    · Newman 대상 API 수: {total_label}")
        print(f"    · Newman 실행 시작: {' '.join(command)}")
        completed = _run_newman_with_progress(command, repo_path, env, total_requests)
        if completed.returncode != 0 and _looks_like_broken_newman_runtime(completed.stdout):
            managed_newman_path = _install_managed_newman(repo_path, env)
            command = _resolve_newman_command_v2(repo_path, newman_config, report_path, env)
            print(f"    · Newman 실행기 복구 후 재시도: {managed_newman_path}")
            completed = _run_newman_with_progress(command, repo_path, env, total_requests)

        effective_report_path = report_path
        if not effective_report_path.is_file() and fallback_report_path.is_file():
            effective_report_path = fallback_report_path
        if not effective_report_path.is_file():
            raise RuntimeError("API 테스트 결과 파일을 찾을 수 없습니다.")
        print("    · Newman 실행 완료")

        output = {
            "runner": runner,
            "base_url": base_url,
            "healthcheck_path": healthcheck_path,
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "report": json.loads(effective_report_path.read_text(encoding="utf-8") or "{}"),
            "authorization_matrix_path": str(authorization_matrix_path) if authorization_matrix_path else "",
            "authorization_matrix": authorization_matrix,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or "Newman 실행이 실패했습니다."
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


def run_api_tests(repo_path: Path, output_path: Path) -> Path:
    config = _load_repo_config(repo_path)
    api_test = config.get("api_test")
    if not isinstance(api_test, dict) or not api_test.get("enabled", True):
        raise RuntimeError("API 테스트 설정이 없거나 비활성화되어 있습니다.")

    runner = str(api_test.get("runner", "newman")).strip().lower()
    if runner != "newman":
        raise RuntimeError(f"지원하지 않는 API 테스트 runner입니다: {runner}")

    start_command = str(api_test.get("start_command", "")).strip()
    base_url = str(api_test.get("base_url", "")).strip()
    if not start_command:
        raise RuntimeError("api_test.start_command 설정이 필요합니다.")
    if not base_url:
        raise RuntimeError("api_test.base_url 설정이 필요합니다.")

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
    for key in ["collection", "environment"]:
        if newman_config.get(key):
            newman_config[key] = str(_resolve_repo_relative_path(repo_path, str(newman_config[key])))
    collection_path = Path(str(newman_config.get("collection", "")))
    environment_path_text = str(newman_config.get("environment") or "").strip()
    if environment_path_text and not Path(environment_path_text).is_file():
        print(f"    · Newman environment 파일을 찾지 못해 옵션을 건너뜁니다: {environment_path_text}")
        newman_config.pop("environment", None)

    runtime_env_path = None
    runtime_env_original = None

    if _should_auto_install_dependencies(repo_path, start_cwd):
        _install_project_dependencies(repo_path, env)
    _initialize_test_database(repo_path, env, api_test, docker_services)
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

    print(f"    · API 서버 실행 시작: {start_command} (cwd={start_cwd})")
    service = _start_background_service(start_command, start_cwd, env)
    stdout_lines, stderr_lines = _start_output_watchers(service)

    try:
        startup_error = None
        try:
            _wait_for_healthcheck_stable(service, stdout_lines, stderr_lines, base_url, healthcheck_path, timeout_seconds, interval_seconds)
        except RuntimeError as error:
            if _looks_like_prisma_client_mismatch(str(error)):
                _generate_prisma_client(repo_path, env)
                print(f"    · API 서버 재시작: {start_command} (cwd={start_cwd})")
                service = _start_background_service(start_command, start_cwd, env)
                stdout_lines, stderr_lines = _start_output_watchers(service)
                _wait_for_healthcheck_stable(service, stdout_lines, stderr_lines, base_url, healthcheck_path, timeout_seconds, interval_seconds)
            elif _looks_like_missing_local_infra(str(error)):
                _start_local_infra_services_with_autostart(repo_path, env, docker_services)
                print(f"    · API 서버 재시작: {start_command} (cwd={start_cwd})")
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
            print(f"    · API 서버 재시작: {start_command} (cwd={start_cwd})")
            service = _start_background_service(start_command, start_cwd, env)
            stdout_lines, stderr_lines = _start_output_watchers(service)
            _wait_for_healthcheck_stable(service, stdout_lines, stderr_lines, base_url, healthcheck_path, timeout_seconds, interval_seconds)

        command = _resolve_newman_command_v2(repo_path, newman_config, report_path, env)
        total_requests = _get_newman_total_requests(collection_path)
        total_label = total_requests if total_requests > 0 else "?"
        print(f"    · Newman 대상 API 수: {total_label}")
        print(f"    · Newman 실행 시작: {' '.join(command)}")
        completed = _run_newman_with_progress(command, repo_path, env, total_requests)
        if completed.returncode != 0 and _looks_like_broken_newman_runtime(completed.stdout):
            managed_newman_path = _install_managed_newman(repo_path, env)
            command = _resolve_newman_command_v2(repo_path, newman_config, report_path, env)
            print(f"    · Newman 실행기 복구 후 재시도: {managed_newman_path}")
            completed = _run_newman_with_progress(command, repo_path, env, total_requests)
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or "Newman 실행이 실패했습니다."
            raise RuntimeError(message)

        effective_report_path = report_path
        if not effective_report_path.is_file() and fallback_report_path.is_file():
            effective_report_path = fallback_report_path
        if not effective_report_path.is_file():
            raise RuntimeError("API 테스트 결과 파일을 찾을 수 없습니다.")
        print("    · Newman 실행 완료")

        output = {
            "runner": runner,
            "base_url": base_url,
            "healthcheck_path": healthcheck_path,
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "report": json.loads(effective_report_path.read_text(encoding="utf-8") or "{}"),
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
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
