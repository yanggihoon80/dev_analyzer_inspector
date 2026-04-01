from pathlib import Path
import os
import shutil
import subprocess


def clone_repo(repo_url: str, branch: str = "main", workspace_dir: str = "workspace") -> Path:
    workspace = Path(workspace_dir).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    repo_name = Path(repo_url.rstrip("/\n").split("/")[-1]).stem
    if not repo_name:
        raise ValueError("Invalid repository URL")

    destination = workspace / repo_name
    if destination.exists():
        counter = 1
        while destination.exists():
            destination = workspace / f"{repo_name}_{counter}"
            counter += 1

    command = ["git", "clone", "--branch", branch, "--single-branch", repo_url, str(destination)]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        message = error.stderr.strip() or error.stdout.strip() or str(error)
        raise RuntimeError(f"Git clone failed: {message}")

    return destination
