import argparse
from pathlib import Path

try:
    from .clone_repo import clone_repo
    from .detect_project import detect_project
    from .normalize import merge_results
    from .render_html import render_report
    from .run_tools import run_bandit, run_eslint, run_semgrep
except ImportError:
    from clone_repo import clone_repo
    from detect_project import detect_project
    from normalize import merge_results
    from render_html import render_report
    from run_tools import run_bandit, run_eslint, run_semgrep


def main() -> None:
    parser = argparse.ArgumentParser(description="Dev Analyzer Inspector MVP")
    parser.add_argument("repo_url", help="Git repository URL to analyze")
    parser.add_argument("--branch", default="main", help="Git branch to clone")
    args = parser.parse_args()

    output_dir = Path("output")
    output_dir.mkdir(parents=True, exist_ok=True)

    repo_path = clone_repo(args.repo_url, args.branch, workspace_dir="workspace")
    project_info = detect_project(repo_path)

    def safe_run(tool_name: str, func, *args):
        try:
            return func(*args)
        except FileNotFoundError as error:
            print(f"Warning: {tool_name} skipped - {error}")
        except RuntimeError as error:
            print(f"Warning: {tool_name} failed - {error}")
        return None

    tool_outputs = {}
    semgrep_path = safe_run("semgrep", run_semgrep, repo_path, output_dir / "semgrep.json")
    if semgrep_path:
        tool_outputs["semgrep"] = semgrep_path

    if project_info.get("js"):
        eslint_path = safe_run("eslint", run_eslint, repo_path, output_dir / "eslint.json")
        if eslint_path:
            tool_outputs["eslint"] = eslint_path

    if project_info.get("python"):
        bandit_path = safe_run("bandit", run_bandit, repo_path, output_dir / "bandit.json")
        if bandit_path:
            tool_outputs["bandit"] = bandit_path

    merged_results = merge_results(tool_outputs, output_dir / "merged_report.json", repo_path)
    render_report(
        merged_results,
        output_dir / "report.html",
        Path(__file__).resolve().parent.parent / "templates",
    )

    print("Analysis completed successfully.")
    print(f"Merged JSON report: {output_dir / 'merged_report.json'}")
    print(f"HTML report: {output_dir / 'report.html'}")
    if tool_outputs:
        print(f"Tools executed: {', '.join(sorted(tool_outputs.keys()))}")
    else:
        print("No static analysis tool output files were generated.")


if __name__ == "__main__":
    main()
