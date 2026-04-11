from pathlib import Path
import os
import shutil
import subprocess
import sys


def _resolve_command(program: str, module: str | None = None) -> list[str]:
    if shutil.which(program):
        return [program]
    if module:
        return [sys.executable, "-m", module]
    raise FileNotFoundError(f"명령어를 찾을 수 없습니다: {program}")


def _run_command(command: list[str], cwd: Path, output_path: Path) -> Path:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
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
        message = completed.stderr.strip() or completed.stdout.strip() or "Tool execution failed"
        raise RuntimeError(message)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(completed.stdout or "{}", encoding="utf-8")
    return output_path


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
