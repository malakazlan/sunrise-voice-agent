# Sunrise voice agent

A Python service that will become a voice agent. It is being built in small slices.

All data used by this project is synthetic. No real customer or personal data is stored or processed.

## What exists today

- `GET /health` returns `{"status": "ok"}`. It is a liveness check only.
- Configuration through environment variables or an optional `.env` file, validated at startup.
- Interactive API docs (`/docs`, `/redoc`, `/openapi.json`) are served only when `APP_ENV=dev`.
- A Postgres database with four tables (`patients`, `medications`, `appointments`,
  `available_slots`), created by an Alembic migration.
- A seed (`python -m app.db.seed`) that loads the synthetic patients and slots from
  `app/db/seed_data.json`, so running it again is safe:
  - A patient that is missing is inserted together with that patient's medications and
    appointment.
  - A patient that already exists is never changed, and nothing is added to it: no
    medication or appointment is inserted for an existing patient, even if a medication or
    appointment of that patient was deleted after an earlier seed.
  - Slots that are missing are added; existing slots are never changed.
  - Nothing is ever deleted. Only the last four digits of an SSN are stored.

### Voice echo call

The server sends the caller's own audio straight back over WebRTC (Pipecat's SmallWebRTC
transport). There is no speech recognition, model or speech synthesis yet.

- `GET /` serves a plain test page: access token, Call and Hang up, a "relay only" switch,
  the connection state, the ICE candidate types in use and a count of audio packets received.
- `GET /ice-servers` returns the ICE server list with short-lived TURN credentials (coturn's
  shared-secret scheme; they expire `MAX_CALL_SECONDS` plus 60 seconds after they are issued).
- `POST /offer` takes an SDP offer (a JSON body of at most 32 KiB) and returns the answer.
- Both need `Authorization: Bearer <DEMO_ACCESS_TOKEN>`. The token is checked before any of
  the body is read. When no token is configured (only allowed in dev without TURN) the two
  endpoints are open.
- At most `MAX_CONCURRENT_CALLS` calls at once (more get 503). A call ends when the caller
  leaves, when it has not connected within 30 seconds, or after `MAX_CALL_SECONDS`.
- coturn runs as a Compose service built from `deploy/coturn`: one configuration file with
  allocation and bandwidth quotas, no relaying to private, loopback, link-local or unspecified
  IPv4 addresses, and its own logging switched off because it records caller addresses.
  Every TURN credential has its own username (`<expiry>:<random id>`), so coturn's per-user
  quota (4 allocations, one call's worth) limits each credential; the total quota is 9 per
  allowed call, and an allocation nobody refreshes ends within 120 seconds. The unspecified
  IPv6 address is not denied: coturn 4.19.2 matches that rule against IPv4 peers as well and
  then refuses every relayed call.

What has been run: the test suite on Windows 10, and `scripts/echo_probe.py` (an aiortc
client that sends a tone and checks it comes back) against the app in Docker Desktop on
Windows, both directly and forced through coturn. The forced-relay runs used the committed
coturn configuration with only its networking changed (`deploy/docker-compose.relay-test.yml`:
app and coturn on 203.0.113.0/24, which coturn does not deny), no peer allowed, coturn on that
network rather than on host networking. CI runs the same forced-relay probe. No browser, phone
or server deployment has been tried yet.

Generated slots: on every run the seed also makes sure the next 14 business days (Monday to
Friday, strictly after today in clinic time, no holiday calendar) have slots for Dr. Patel at
9:30 AM, 11:00 AM and 3:00 PM and for Dr. Lee at 10:00 AM and 1:30 PM, America/New_York time.
These rows have `source = 'generated'`; the slots from the task data have `source = 'task'`.
A generated slot never replaces an existing slot at the same doctor and time.

## Configuration

| Variable            | Allowed values                      | Default        |
|---------------------|-------------------------------------|----------------|
| `APP_ENV`           | `dev`, `demo`, `prod`               | none, required |
| `LOG_LEVEL`         | `DEBUG`, `INFO`, `WARNING`, `ERROR` | `INFO`         |
| `POSTGRES_HOST`     | host name                           | none, required |
| `POSTGRES_PORT`     | port                                | `5432`         |
| `POSTGRES_DB`       | database name                       | none, required |
| `POSTGRES_USER`     | user name                           | none, required |
| `POSTGRES_PASSWORD` | password, never logged (see below)  | none, required |
| `MAX_CONCURRENT_CALLS` | 1 to 20, digits only             | `3`            |
| `MAX_CALL_SECONDS`  | 1 to 3600                           | `600`          |
| `TURN_HOST`         | host name, no scheme or port        | required unless dev |
| `TURN_SHARED_SECRET`| 32+ of `A-Z a-z 0-9 . _ ~ + / = -`, never logged | required unless dev |
| `DEMO_ACCESS_TOKEN` | at least 32 characters, never logged | required unless dev |

Settings come from the environment or `.env`. An invalid or missing value stops startup, and
an empty value counts as unset. Environment variables take precedence over `.env`. The same
`POSTGRES_*` values configure the database container in Compose, so there is one set of
database credentials.

Unless `APP_ENV=dev`, `POSTGRES_PASSWORD` must be at least 16 characters and must not be the
placeholder from `.env.example`; startup stops otherwise.

`TURN_HOST` and `TURN_SHARED_SECRET` go together, and configuring TURN requires
`DEMO_ACCESS_TOKEN` in every environment, so TURN credentials are never handed out without the
token. Generate the two secrets with `openssl rand -hex 32`.

## Run with Docker

```sh
cp .env.example .env          # then choose your own POSTGRES_PASSWORD
docker compose --env-file .env -f deploy/docker-compose.yml up --build
curl http://127.0.0.1:8000/health
```

The app container runs `alembic upgrade head`, then the seed, then the server. A failure in
any step stops the container. The database is not published to the host.

`--env-file .env` is required: Compose takes every value from it and refuses to start, naming
the variable, if `APP_ENV` or a database credential is missing. Each container receives only
its own variables: the database its three credentials, the app its settings, coturn
`TURN_HOST`, `TURN_SHARED_SECRET` and `MAX_CONCURRENT_CALLS`. coturn checks those three by the
same rules as the app and stops at start-up, naming the variable, if one is missing or not
valid, so the whole stack needs the TURN values set. coturn uses host networking, which needs a
Linux host; it listens on 3478 (UDP and TCP) and relays on UDP 49160-49359. The app is
published on `127.0.0.1:8000` only.

## Run locally with uv

Requires [uv](https://docs.astral.sh/uv/) and Docker for the database. The dev Compose file
publishes the database on `127.0.0.1:5432` only.

```sh
cp .env.example .env          # POSTGRES_HOST=127.0.0.1
uv sync
docker compose --env-file .env -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml up -d db
uv run alembic upgrade head
uv run python -m app.db.seed
uv run uvicorn app.main:create_app --factory --port 8000 --no-access-log
curl http://127.0.0.1:8000/health
```

Checks:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict app tests scripts
uv run pytest
```

Echo probe, against a running app (`--relay` restricts it to TURN relay candidates; the token,
if one is set, comes from the `PROBE_TOKEN` environment variable):

```sh
uv run python scripts/echo_probe.py --url http://127.0.0.1:8000 --calls 2
```

The tests need a running database server and read its address from the `POSTGRES_*`
environment variables (not from `.env`), for example after exporting the values from your
`.env`. The user must be allowed to create databases: each test gets its own database,
migrated with Alembic, and drops it afterwards.
