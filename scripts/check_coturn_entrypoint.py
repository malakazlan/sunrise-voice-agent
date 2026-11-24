"""Run coturn's entrypoint against every value in deploy/coturn/validation-cases.json.

The image's turnserver is replaced by a stub that prints a marker, its arguments and the
config file it was given, so each case shows whether the entrypoint started coturn and with
what. The app side of the same table is
tests/test_config.py::test_app_and_coturn_accept_the_same_values.

    python scripts/check_coturn_entrypoint.py --image <the built coturn image>

The stub is written inside the container, by the container's own user, into a new directory
under /tmp, so it does not depend on host file ownership or modes (a bind-mounted host
directory can be closed to the container's user on Linux). Every case must show the stub's
marker; if it is missing the real turnserver, or nothing, ran instead and the run stops there.
Every container run is bounded; on timeout the container is removed and the run fails.

Exit code 0 means every case behaved as the table says.
"""

import argparse
import json
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COTURN_DIR = REPO_ROOT / "deploy" / "coturn"
CONFIG = "/tmp/turnserver.conf"  # noqa: S108 (the path inside the coturn container)
# Seconds one container run may take; each finishes in about one second.
RUN_TIMEOUT = 30
# Valid by both sides' rules (32 characters or more, at least 10 different), so that each case
# changes one variable only.
VALID = {
    "TURN_HOST": "turn.example.test",
    "TURN_SHARED_SECRET": "abcdefghij" * 4,
    "MAX_CONCURRENT_CALLS": "3",
}
# Run in the container: write the stub where the container user can run it, put it first on
# PATH, then run the given command. The stub itself arrives in CHECK_STUB.
INSTALL_STUB = (
    "set -e; stub_dir=$(mktemp -d /tmp/stub.XXXXXX); "
    'printf "%s" "$CHECK_STUB" > "$stub_dir/turnserver"; '
    'chmod 0755 "$stub_dir/turnserver"; unset CHECK_STUB; '
    'PATH="$stub_dir:$PATH"; export PATH; '
)


class StubDidNotRunError(Exception):
    """The container ran something other than the stub, or ran too long."""


def stub_script(marker: str) -> str:
    return (
        f'#!/bin/sh\necho "{marker}"\necho "turnserver $*"\necho "--- config"\n'
        'if [ -n "${2:-}" ]; then cat "$2"; fi\n'
    )


def run_in_container(
    docker: str, image: str, marker: str, env: dict[str, str], command: str
) -> tuple[int, str, str]:
    name = f"coturn-entrypoint-check-{secrets.token_hex(6)}"
    args = [docker, "run", "--rm", "--name", name, "-e", f"CHECK_STUB={stub_script(marker)}"]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    args += ["--entrypoint", "/bin/sh", image, "-c", INSTALL_STUB + command]
    try:
        # Built from the fixed table and the image name given on the command line.
        done = subprocess.run(  # noqa: S603
            args, capture_output=True, text=True, check=False, timeout=RUN_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        # Stopping the client does not stop the container; remove it by name.
        subprocess.run(  # noqa: S603
            [docker, "rm", "-f", name], capture_output=True, check=False, timeout=RUN_TIMEOUT
        )
        raise StubDidNotRunError(
            f"no result within {RUN_TIMEOUT} s; container {name} removed. Whatever was started"
            " as turnserver did not exit, so it was not the stub (the real turnserver runs until"
            " stopped)"
        ) from None
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


def check(docker: str, image: str, marker: str, case: dict[str, object]) -> str | None:
    """None when the case behaves as the table says, else what went wrong."""
    name, value = str(case["name"]), str(case["value"])
    env = {**VALID, name: value}
    code, out, err = run_in_container(
        docker, image, marker, env, "exec /bin/sh /usr/local/bin/sunrise-coturn"
    )
    lines = out.splitlines()
    if not case["accepted"]:
        if code == 0 or marker in lines:
            return "accepted a value the table refuses"
        if err != f"{name} is not valid\n":
            return f"refused with an unexpected message ({len(err)} characters)"
        return None
    if code != 0:
        return f"refused a value the table accepts (exit {code})"
    if lines[:1] != [marker]:
        raise StubDidNotRunError(
            "the entrypoint exited 0 but the stub's marker is missing, so the stub did not run"
        )
    if lines[1:3] != [f"turnserver -c {CONFIG}", "--- config"]:
        return "coturn was not started with the one config file"
    calls = int(str(case.get("calls", VALID["MAX_CONCURRENT_CALLS"])))
    if lines[3:] != expected_config(env, calls):
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
    # A fresh marker per run, so no output left from elsewhere can pass for the stub's.
    marker = f"STUB-TURNSERVER-RAN-{secrets.token_hex(8)}"

    failures = 0
    try:
        # Before any case: turnserver, looked up on PATH as the entrypoint looks it up, is the stub.
        code, out, _err = run_in_container(docker, args.image, marker, {}, "exec turnserver")
        if code != 0 or out.splitlines()[:1] != [marker]:
            raise StubDidNotRunError("turnserver on the container's PATH is not the stub")
        print("ok   the stub is the turnserver the container runs")
        for case in cases:
            problem = check(docker, args.image, marker, case)
            verdict = "accepts" if case["accepted"] else "refuses"
            label = f"{case['name']}={case['value']!r}"
            print(
                f"{'ok  ' if problem is None else 'FAIL'} {verdict} {label}"
                + (f": {problem}" if problem else "")
            )
            failures += problem is not None
    except StubDidNotRunError as error:
        print(f"FAIL the stub did not run: {error}. Stopping: no case can be judged.")
        return 1
    print(f"{len(cases) - failures}/{len(cases)} cases as expected")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
