# Sunrise voice agent

A Python service that will become a voice agent. It is being built in small slices.

All data used by this project is synthetic. No real customer or personal data is stored or processed.

## What exists today

- `GET /health` returns `{"status": "ok"}`. It is a liveness check only.
- Configuration through environment variables or an optional `.env` file, validated at startup.
- Interactive API docs (`/docs`, `/redoc`, `/openapi.json`) are served only when `APP_ENV=dev`.

Nothing else is built yet.

## Configuration

| Variable    | Allowed values                     | Default |
|-------------|------------------------------------|---------|
| `APP_ENV`   | `dev`, `demo`, `prod`              | `dev`   |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` | `INFO`  |

Copy `.env.example` to `.env` to override values. An invalid value stops startup.
Environment variables take precedence over `.env`. When the app reads `.env` itself (the uv
route below), a key in it that is not a setting also stops startup.

## Run locally with uv

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run uvicorn app.main:app --port 8000
curl http://127.0.0.1:8000/health
```

Checks:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict app tests
uv run pytest
```

## Run with Docker

```sh
docker compose -f deploy/docker-compose.yml up --build
curl http://127.0.0.1:8000/health
```

Compose passes `.env` to the container as environment variables when the file exists. The
image does not contain `.env`, so in this route an unknown key is not detected.
