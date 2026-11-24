"""Run coturn's entrypoint against every value in deploy/coturn/validation-cases.json.

The image's turnserver is replaced by a stub that prints its arguments and the config file
it was given, so each case shows whether the entrypoint started coturn and with what. The app
side of the same table is tests/test_config.py::test_app_and_coturn_accept_the_same_values.

    python scripts/check_coturn_entrypoint.py --image <the built coturn image>

Exit code 0 means every case behaved as the table says.
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COTURN_DIR = REPO_ROOT / "deploy" / "coturn"
CONFIG = "/tmp/turnserver.conf"  # noqa: S108 (the path inside the coturn container)
STUB = '#!/bin/sh\necho "STUB turnserver $*"\necho "--- config"\ncat "$2"\n'
VALID = {
    "TURN_HOST": "turn.example.test",
    "TURN_SHARED_SECRET": "a" * 32,
    "MAX_CONCURRENT_CALLS": "3",
}


def run_entrypoint(
    docker: str, image: str, stub_dir: Path, env: dict[str, str]
) -> tuple[int, str, str]:
    command = [docker, "run", "--rm", "-v", f"{stub_dir}:/stub:ro"]
    for name, value in env.items():
        command += ["-e", f"{name}={value}"]
    command += [
        "--entrypoint",
        "/bin/sh",
        image,
        "-c",
        "PATH=/stub:$PATH; export PATH; exec /bin/sh /usr/local/bin/sunrise-coturn",
    ]
    # The command is built from the fixed table and the image name given on the command line.
    done = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
    return done.returncode, done.stdout, done.stderr


def expected_config(env: dict[str, str], calls: int) -> list[str]:
    base = (COTURN_DIR / "turnserver.conf").read_text(encoding="utf-8").splitlines()
    total = 9 * calls
    return [
        *base,
        f"realm={env['TURN_HOST']}",
        "user-quota=4",
        f"total-quota={total}",
        "max-bps=64000",
        f"bps-capacity={total * 64000}",
        f"static-auth-secret={env['TURN_SHARED_SECRET']}",
    ]


def check(docker: str, image: str, stub_dir: Path, case: dict[str, object]) -> str | None:
    """None when the case behaves as the table says, else what went wrong."""
    name, value = str(case["name"]), str(case["value"])
    env = {**VALID, name: value}
    code, out, err = run_entrypoint(docker, image, stub_dir, env)
    if not case["accepted"]:
        if code == 0 or "STUB" in out:
            return "accepted a value the table refuses"
        if err != f"{name} is not valid\n":
            return f"refused with an unexpected message ({len(err)} characters)"
        return None
    if code != 0:
        return f"refused a value the table accepts (exit {code})"
    lines = out.splitlines()
    if lines[:2] != [f"STUB turnserver -c {CONFIG}", "--- config"]:
        return "coturn was not started with the one config file"
    calls = int(str(case.get("calls", VALID["MAX_CONCURRENT_CALLS"])))
    if lines[2:] != expected_config(env, calls):
        return "the config file differs from the committed settings plus the six expected lines"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    docker = shutil.which("docker")
    if docker is None:
        print("docker is not on PATH")
        return 1
    cases = json.loads((COTURN_DIR / "validation-cases.json").read_text(encoding="utf-8"))["cases"]

    failures = 0
    with tempfile.TemporaryDirectory() as stub:
        stub_dir = Path(stub)
        turnserver = stub_dir / "turnserver"
        turnserver.write_text(STUB, encoding="utf-8", newline="\n")
        turnserver.chmod(0o755)
        for case in cases:
            problem = check(docker, args.image, stub_dir, case)
            verdict = "accepts" if case["accepted"] else "refuses"
            label = f"{case['name']}={case['value']!r}"
            print(
                f"{'ok  ' if problem is None else 'FAIL'} {verdict} {label}"
                + (f": {problem}" if problem else "")
            )
            failures += problem is not None
    print(f"{len(cases) - failures}/{len(cases)} cases as expected")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
