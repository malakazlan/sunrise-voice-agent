import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_env_file_is_gitignored() -> None:
    git = shutil.which("git")
    assert git is not None, "git must be on PATH"

    result = subprocess.run(
        [git, "check-ignore", "--verbose", ".env"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    # The rule must come from the repo's own .gitignore, not a developer's global excludes.
    assert result.stdout.startswith(".gitignore:"), result.stdout
