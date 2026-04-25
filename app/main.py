import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

try:
    from .clone_repo import clone_repo
    from .detect_project import detect_project
    from .normalize import merge_results
    from .render_html import render_report
    from .run_tools import ensure_repo_config_exists, get_repo_config_path, has_api_test_config, run_api_tests, run_bandit, run_eslint, run_semgrep
except ImportError:
    from clone_repo import clone_repo
    from detect_project import detect_project
    from normalize import merge_results
    from render_html import render_report
    from run_tools import ensure_repo_config_exists, get_repo_config_path, has_api_test_config, run_api_tests, run_bandit, run_eslint, run_semgrep


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _env_value(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    return value or default


def _parse_analysis_targets() -> set[str]:
    raw = os.getenv("ANALYSIS_TARGETS", "static,api")
    values = {item.strip().lower() for item in raw.split(",") if item.strip()}
    aliases = {
        "static_analysis": "static",
        "static-code": "static",
        "static_code": "static",
        "api_test": "api",
        "api_tests": "api",
    }
    normalized = {aliases.get(value, value) for value in values}
    valid = {"static", "api"}
    return normalized & valid if normalized else {"static", "api"}


def main() -> None:
    default_repo_url = _env_value("REPO_URL", "")
    default_branch = _env_value("GIT_BRANCH", "main")
    workspace_dir = _env_value("WORKSPACE_DIR", "workspace")
    output_dir = Path(_env_value("OUTPUT_DIR", "output"))

    parser = argparse.ArgumentParser(description="Dev Analyzer Inspector MVP")
    parser.add_argument("repo_url", nargs="?", default=default_repo_url, help="Git repository URL to analyze")
    parser.add_argument("--branch", default=default_branch, help="Git branch to clone")
    args = parser.parse_args()

    if not args.repo_url:
        raise ValueError("repo_url 인자가 없고 .env의 REPO_URL도 설정되지 않았습니다.")

    output_dir.mkdir(parents=True, exist_ok=True)

    repo_path = clone_repo(args.repo_url, args.branch, workspace_dir=workspace_dir)
    print(f"[1/6] 분석 대상 준비 완료: {repo_path}")
    project_info = detect_project(repo_path)
    print(f"[2/6] 프로젝트 유형 감지 완료: python={project_info.get('python', False)}, js={project_info.get('js', False)}")
    analysis_targets = _parse_analysis_targets()
    print(f"[3/6] 활성 분석 대상: {', '.join(sorted(analysis_targets))}")
    api_test_status = {
        "enabled": "api" in analysis_targets,
        "configured": False,
        "ran": False,
        "reason": "",
    }

    def safe_run(tool_name: str, func, *args):
        print(f"  - {tool_name} 실행 시작")
        try:
            result = func(*args)
            print(f"  - {tool_name} 실행 완료")
            return result
        except FileNotFoundError as error:
            print(f"경고: {tool_name} 실행을 건너뜁니다 - {error}")
        except RuntimeError as error:
            print(f"경고: {tool_name} 실행 실패 - {error}")
        return None

    def _clear_output(path: Path) -> None:
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass

    tool_outputs = {}
    if "static" in analysis_targets:
        print("[4/6] 정적 분석 시작")
        _clear_output(output_dir / "semgrep.json")
        semgrep_path = safe_run("semgrep", run_semgrep, repo_path, output_dir / "semgrep.json")
        if semgrep_path:
            tool_outputs["semgrep"] = semgrep_path

        if project_info.get("js"):
            _clear_output(output_dir / "eslint.json")
            eslint_path = safe_run("eslint", run_eslint, repo_path, output_dir / "eslint.json")
            if eslint_path:
                tool_outputs["eslint"] = eslint_path

        if project_info.get("python"):
            _clear_output(output_dir / "bandit.json")
            bandit_path = safe_run("bandit", run_bandit, repo_path, output_dir / "bandit.json")
            if bandit_path:
                tool_outputs["bandit"] = bandit_path
        print("[4/6] 정적 분석 종료")
    else:
        print("[4/6] 정적 분석 건너뜀")

    try:
        config_path = None
        if "api" in analysis_targets:
            print("[5/6] API 테스트 설정 확인 및 준비 시작")
            config_path = ensure_repo_config_exists(repo_path)
        else:
            config_path = get_repo_config_path(repo_path)

        api_test_enabled = "api" in analysis_targets and has_api_test_config(repo_path)
        api_test_status["configured"] = bool(api_test_enabled)
        if config_path is not None and api_test_status["enabled"]:
            api_test_status["config_path"] = str(config_path)
        if config_path is not None and not api_test_enabled:
            api_test_status["reason"] = f"설정 파일은 찾았지만 api_test 섹션이 없거나 비활성화되어 있습니다: {config_path.name}"
    except Exception as error:
        print(f"경고: api_test 설정 확인 실패 - {error}")
        api_test_enabled = False
        api_test_status["reason"] = f"api_test 설정 확인 실패: {error}"

    if not api_test_status["enabled"]:
        api_test_status["reason"] = "ANALYSIS_TARGETS 설정에서 api가 비활성화되어 있습니다."
    elif not api_test_enabled and not api_test_status["reason"]:
        api_test_status["reason"] = "분석 대상 저장소에서 dev-analyzer 설정 파일을 찾지 못했습니다."

    if api_test_enabled:
        print("[5/6] API 테스트 시작")
        _clear_output(output_dir / "api_test.json")
        api_test_path = safe_run("api_test", run_api_tests, repo_path, output_dir / "api_test.json")
        if api_test_path:
            tool_outputs["api_test"] = api_test_path
            api_test_status["ran"] = True
        else:
            api_test_status["reason"] = "API 테스트 실행에 실패했거나 결과 파일이 생성되지 않았습니다."
        print("[5/6] API 테스트 종료")
    else:
        print(f"[5/6] API 테스트 건너뜀: {api_test_status['reason']}")

    print("[6/6] 통합 리포트 생성 시작")
    merged_results = merge_results(tool_outputs, output_dir / "merged_report.json", repo_path)
    render_report(
        merged_results,
        output_dir / "report.html",
        Path(__file__).resolve().parent.parent / "templates",
        tool_outputs=tool_outputs,
        report_context={
            "api_test_status": api_test_status,
            "analysis_targets": sorted(analysis_targets),
            "repo_path": str(repo_path),
        },
    )
    print("[6/6] 통합 리포트 생성 완료")

    print("분석이 성공적으로 완료되었습니다.")
    print(f"통합 JSON 리포트: {output_dir / 'merged_report.json'}")
    print(f"HTML 리포트: {output_dir / 'report.html'}")
    if tool_outputs:
        print(f"실행된 도구: {', '.join(sorted(tool_outputs.keys()))}")
    else:
        print("생성된 정적 분석 결과 파일이 없습니다.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("오류: 사용자에 의해 실행이 중단되었습니다.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"오류: {error}", file=sys.stderr)
        raise SystemExit(1)
