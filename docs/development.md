# Development, Compose details and checks

Part of the [README](../README.md)'s reference.

## Compose

Six services start: `db`; `startup`, a one-shot run from the app's image that checks every
setting of the deployment together (`python -m app.config`), then, as the owner, runs `alembic
upgrade head`, the role set-up (`python -m app.db.roles`) and the seed, and exits; `app`, the
server of the calls (`APP_ROLE=voice`), which starts only after `startup` has exited with 0 and
connects as the runtime role; `admin-api`, the admin API (`APP_ROLE=admin`), from the same image,
started the same way and published on `127.0.0.1:8001`; `coturn`; and `console`, the admin
console, published on `127.0.0.1:3000`, which depends on nothing. A failure in any start-up
step ends `startup` with a non-zero code and neither `app` nor `admin-api` is started (`docker
compose up` reports it and exits non-zero). `startup` shows as exited once it is done; that is
its normal state. The database is not published to the host. Without `ADMIN_PASSWORD`,
`admin-api` still starts and answers `/health`, with the admin API off. Without
`ADMIN_API_ORIGIN` in the shell, `console` answers 503 to everything.

Restarts: `docker compose up -d` after `stop` runs `startup` again before `app`; on a running
stack it runs `startup` again and recreates `app` only if its configuration
changed, as after a password rotation. Restarting only the app (`docker compose restart app`)
does not need `startup`.
`docker compose restart` with no service restarts `startup` and `app` together without waiting
for `startup` to finish, so use `up -d` to restart the stack.

`--env-file .env` is required: Compose takes every value from it and refuses to start, naming
the variable, if `APP_ENV` or a database credential is missing. Each container receives only
its own variables: the table is in the README's Configuration.

coturn checks its three by the
same rules as the app and stops at start-up, naming the variable, if `TURN_HOST` or
`TURN_SHARED_SECRET` is missing or any of the three is not valid; a missing or empty
`MAX_CONCURRENT_CALLS` means 3, as in the app. So the whole stack needs the TURN values set. coturn uses host networking, which needs a
Linux host; it listens on 3478 (UDP and TCP) and relays on UDP 49160-49359. The app is
published on `127.0.0.1` only, port 8000 by default; on a server it is reached through the
HTTPS proxy.

When port 8000 is taken on the host, as on a server whose own dashboard uses it, set
`APP_HOST_PORT` in the shell: `APP_HOST_PORT=8100 docker compose --env-file .env -f
deploy/docker-compose.yml up --build` publishes the app on `127.0.0.1:8100`. It is a Compose
variable only, not an application setting, and no container receives it, so it does not go in
`.env`, whose unknown keys the app refuses. `ADMIN_API_HOST_PORT` (default 8001) does the same
for `admin-api`.

## Run locally with uv

Requires [uv](https://docs.astral.sh/uv/) and Docker for the database. The dev Compose file
publishes the database on `127.0.0.1:5432` only.

```sh
cp .env.example .env          # POSTGRES_HOST=127.0.0.1
uv sync
docker compose --env-file .env -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml up -d db
uv run alembic upgrade head
uv run python -m app.db.roles
uv run python -m app.db.seed
uv run uvicorn app.main:create_app --factory --port 8000 --no-access-log
curl http://127.0.0.1:8000/health
```

Checks:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict app tests scripts evals
uv run pytest
```

Relay probe, against a running app (`--relay` restricts it to TURN relay candidates; the
token, if one is set, comes from the `PROBE_TOKEN` environment variable). Each call starts
speech-to-text, so run it with real keys, or where the vendor hosts cannot be reached:

```sh
uv run python scripts/relay_probe.py --url http://127.0.0.1:8000 --calls 2
```

The tests need a running database server and read its address from the `POSTGRES_*`
environment variables (not from `.env`), for example after exporting the values from your
`.env`. The user must be a superuser: each test gets its own database, migrated with Alembic,
and drops it afterwards, and the audit tamper tests switch the audit table's triggers off for
one transaction to show that the chain catches edits. The tests use their own audit key.
Each test run also creates its own runtime role (`sva_test_<random>_app`, a random password)
and sets it up in every test database, so tests of the application run as that role; set-up,
schema checks, the seed and the tamper fixture use the owner. Roles belong to the whole
server, so every run and every role test uses a name of its own, and the role is dropped when
the run ends; one left behind by a crashed run never meets another run.
