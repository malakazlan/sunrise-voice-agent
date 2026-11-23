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
- Pure decision functions in `app/domain/` (no database, network or settings file):
  - `office_hours.is_open`: Monday to Friday, 09:00 up to but not including 17:00, clinic
    time; `office_hours.clinic_now` gives the current or simulated time.
  - `matching.pick_patient`: which of the records already matched on date of birth and SSN
    last four the claimed name belongs to. One exact match wins, otherwise one match that
    sounds the same; two at the same level is no match. `matching.name_key` is the phonetic
    key for a claimed name.
  - `matching.match_medication`: which of the caller's own medications a spoken name means,
    ignoring strength and form words and absorbing one-letter speech-to-text errors; two
    matches is no match.

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
| `SIMULATED_NOW`     | ISO date and time, or empty         | empty (unset)  |
| `POSTGRES_HOST`     | host name                           | none, required |
| `POSTGRES_PORT`     | port                                | `5432`         |
| `POSTGRES_DB`       | database name                       | none, required |
| `POSTGRES_USER`     | user name                           | none, required |
| `POSTGRES_PASSWORD` | password, never logged (see below)  | none, required |

Settings come from the environment or `.env`. An invalid or missing value stops startup.
Environment variables take precedence over `.env`. The same `POSTGRES_*` values configure
the database container in Compose, so there is one set of database credentials.

Unless `APP_ENV=dev`, `POSTGRES_PASSWORD` must be at least 16 characters and must not be the
placeholder from `.env.example`; startup stops otherwise.

`SIMULATED_NOW` fixes the clinic clock for dev and demo, for example to show the office
closed on a weekday afternoon. Empty means the real clock. A value without a zone, such as
`2026-11-24T10:30:00`, is clinic time (America/New_York); a wall time that a clock change
skips or repeats is refused, so give an offset for those. Startup stops if it is set and
`APP_ENV` is `prod`.

## Run with Docker

```sh
cp .env.example .env          # then choose your own POSTGRES_PASSWORD
docker compose --env-file .env -f deploy/docker-compose.yml up --build
curl http://127.0.0.1:8000/health
```

The app container runs `alembic upgrade head`, then the seed, then the server. A failure in
any step stops the container. The database is not published to the host.

`--env-file .env` is required: Compose takes `POSTGRES_DB`, `POSTGRES_USER` and
`POSTGRES_PASSWORD` from it and refuses to start, naming the variable, if one is missing. The
database container receives only those three variables; the app receives all of `.env`.

## Run locally with uv

Requires [uv](https://docs.astral.sh/uv/) and Docker for the database. The dev Compose file
publishes the database on `127.0.0.1:5432` only.

```sh
cp .env.example .env          # POSTGRES_HOST=127.0.0.1
uv sync
docker compose --env-file .env -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml up -d db
uv run alembic upgrade head
uv run python -m app.db.seed
uv run uvicorn app.main:create_app --factory --port 8000
curl http://127.0.0.1:8000/health
```

Checks:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict app tests
uv run pytest
```

The tests need a running database server and read its address from the `POSTGRES_*`
environment variables (not from `.env`), for example after exporting the values from your
`.env`. The user must be allowed to create databases: each test gets its own database,
migrated with Alembic, and drops it afterwards.
