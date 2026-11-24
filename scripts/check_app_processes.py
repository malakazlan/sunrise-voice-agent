"""Check, inside the running app container, that no process there holds the owner's identity.

CI runs it in the app container as the container's own user, the user its server and its
HEALTHCHECK run as:

    printf '%s' "$owner_password" | docker compose ... exec -T app \
        python -c "$(cat scripts/check_app_processes.py)" OWNER_USER SECONDS

It samples every readable /proc/<pid>/environ, about every 5 ms, until it has seen a
HEALTHCHECK process (at most SECONDS), and then:

- no process it saw has a variable that only the start-up settings have (the owner's);
- the server (PID 1) and every HEALTHCHECK it saw have the runtime identity, so the reads were
  real and not empty;
- the owner's login, tried with every value any of those processes holds, is refused each time,
  while the owner's real password (read from stdin, never from an environment or a command
  line, so it is in no process this check reads) is accepted, so the attempt itself can work.

It prints names and counts only, never a value. Exit status 1 on any failure.
"""

import os
import sys
import time

import psycopg

from app.config import Settings, StartupSettings

# What the image's HEALTHCHECK runs (Dockerfile): a request to the server's /health.
HEALTHCHECK = b"urllib.request.urlopen('http://127.0.0.1:8000/health'"
LOGIN_REFUSALS = ("password authentication failed", "no password supplied")


def variables(settings_class: type[Settings] | type[StartupSettings]) -> set[str]:
    return {name.upper() for name in settings_class.model_fields}


def read_processes(skip: int) -> dict[int, tuple[bytes, dict[str, str]]]:
    """Every process whose command line and environment this user may read, but `skip`."""
    found = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == skip:
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as file:
                command = file.read()
            with open(f"/proc/{entry}/environ", "rb") as file:
                raw = file.read()
        except OSError:
            # Gone already, or not this user's to read.
            continue
        environment = dict(item.decode().split("=", 1) for item in raw.split(b"\0") if b"=" in item)
        found[int(entry)] = (command, environment)
    return found


def owner_login(host: str, port: str, database: str, user: str, password: str) -> bool:
    try:
        psycopg.connect(
            host=host, port=port, dbname=database, user=user, password=password, connect_timeout=10
        ).close()
    except psycopg.OperationalError as error:
        if not any(refusal in str(error) for refusal in LOGIN_REFUSALS):
            raise SystemExit("an owner login failed for a reason other than its password") from None
        return False
    return True


def main() -> None:
    owner_user, seconds = sys.argv[1], float(sys.argv[2])
    owner_password = sys.stdin.read()
    owner_only = variables(StartupSettings) - variables(Settings)
    if owner_only != {"POSTGRES_USER", "POSTGRES_PASSWORD"}:
        raise SystemExit(f"unexpected owner-only variables: {' '.join(sorted(owner_only))}")

    seen: dict[int, tuple[bytes, dict[str, str]]] = {}
    healthchecks: set[int] = set()
    deadline = time.monotonic() + seconds
    while not healthchecks and time.monotonic() < deadline:
        for pid, process in read_processes(skip=os.getpid()).items():
            seen[pid] = process
            if HEALTHCHECK in process[0]:
                healthchecks.add(pid)
        time.sleep(0.005)
    print(f"processes read: {len(seen)}; healthchecks seen: {len(healthchecks)}")
    if not healthchecks:
        raise SystemExit(f"no HEALTHCHECK process was seen within {seconds:.0f}s")
    if 1 not in seen or b"uvicorn" not in seen[1][0]:
        raise SystemExit("PID 1 is not the server, or could not be read")

    failed = False
    for pid, (_command, environment) in sorted(seen.items()):
        if environment.keys() & owner_only:
            names = " ".join(sorted(environment.keys() & owner_only))
            print(f"process {pid} holds {names}")
            failed = True
    for pid in sorted({1} | healthchecks):
        if "APP_DB_USER" not in seen[pid][1]:
            print(f"process {pid} has no APP_DB_USER: the read was not real")
            failed = True
    print("server environment names:", " ".join(sorted(seen[1][1].keys() & variables(Settings))))

    server = seen[1][1]
    target = (server["POSTGRES_HOST"], server["POSTGRES_PORT"], server["POSTGRES_DB"], owner_user)
    if not owner_login(*target, owner_password):
        raise SystemExit("the owner's own password is refused: the login attempt does not work")
    values = {value for _command, environment in seen.values() for value in environment.values()}
    accepted = sum(owner_login(*target, value) for value in values)
    print(f"owner logins with each of the {len(values)} values these processes hold: {accepted}")
    if accepted or failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
