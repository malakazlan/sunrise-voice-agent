import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def check_ignore(*args: str) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    assert git is not None, "git must be on PATH"
    return subprocess.run(
        [git, "check-ignore", *args, ".env"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_env_file_is_gitignored() -> None:
    # Exit 0 from the quiet form means ignored. The verbose form also exits 0 when the
    # matching pattern is a negation, so it cannot be used for the decision.
    decision = check_ignore("--quiet")
    assert decision.returncode == 0, decision.stderr

    # The rule must come from the repo's own .gitignore, not a developer's global excludes.
    # Verbose output is "<source>:<line>:<pattern>\t<path>".
    match = check_ignore("--verbose").stdout.split("\t")[0]
    source, _line, pattern = match.split(":", 2)
    assert source == ".gitignore", match
    assert not pattern.startswith("!")
