from pathlib import Path


def detect_project(root_path: Path) -> dict:
    root = Path(root_path)
    has_js = (root / "package.json").is_file()
    has_python = (root / "requirements.txt").is_file() or any(root.rglob("*.py"))
    return {"python": has_python, "js": has_js}
