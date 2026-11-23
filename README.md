# Sunrise voice agent

A Python service that will become a voice agent. It is being built in small slices.

All data used by this project is synthetic. No real customer or personal data is stored or processed.

## What exists today

- `GET /health` returns `{"status": "ok"}`. It is a liveness check only.
- A voice echo call over WebRTC: the server sends the caller's own audio straight back.
  There is no speech recognition, model or speech synthesis yet.
  - `GET /` serves a plain test page: access token, Call and Hang up, the connection state,
    the ICE candidate types in use and a count of audio packets received.
  - `GET /ice-servers` returns the browser's ICE server list with short-lived TURN credentials.
  - `POST /offer` takes the browser's SDP offer and returns the server's answer.
  - Both endpoints need `Authorization: Bearer <DEMO_ACCESS_TOKEN>` (no token is checked when
    none is configured, which is only allowed in dev without TURN). Calls beyond
    `MAX_CONCURRENT_CALLS` get 503; a call is ended after `MAX_CALL_SECONDS`.
- A coturn TURN server in the Compose file, in shared-secret mode.
- Configuration through environment variables or an optional `.env` file, validated at startup.
- Interactive API docs (`/docs`, `/redoc`, `/openapi.json`) are served only when `APP_ENV=dev`.

## Configuration

| Variable               | Allowed values                      | Default                    |
|------------------------|-------------------------------------|----------------------------|
| `APP_ENV`              | `dev`, `demo`, `prod`               | none, required             |
| `LOG_LEVEL`            | `DEBUG`, `INFO`, `WARNING`, `ERROR` | `INFO`                     |
| `DEMO_ACCESS_TOKEN`    | any string (secret)                 | required unless dev        |
| `MAX_CONCURRENT_CALLS` | integer, at least 1                 | `3`                        |
| `MAX_CALL_SECONDS`     | integer, at least 1                 | `600`                      |
| `TURN_HOST`            | host name, no scheme or port        | required unless dev        |
| `TURN_SHARED_SECRET`   | coturn shared secret (secret)       | required unless dev        |

Settings come from the environment or `.env`, and `APP_ENV` is required. An invalid or
missing value stops startup. Environment variables take precedence over `.env`, and empty
values count as unset. `TURN_HOST` and `TURN_SHARED_SECRET` go together, and configuring TURN
requires `DEMO_ACCESS_TOKEN` in every environment, so TURN credentials are never handed out
without the token. Generate secrets with `openssl rand -hex 32` (use hex: coturn's container
entrypoint passes the secret through the shell).

## Run locally with uv

Requires [uv](https://docs.astral.sh/uv/). This works on Windows, macOS and Linux.

```sh
cp .env.example .env
uv sync
uv run uvicorn app.main:create_app --factory --port 8000 --no-access-log
curl http://127.0.0.1:8000/health
```

Open `http://localhost:8000/` for the call page. Browsers allow the microphone only on
`localhost` or over HTTPS. In dev without TURN the call uses host candidates, so it works on
the same machine or network.

Checks:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict app tests scripts
uv run pytest
```

Echo probe: a WebRTC client (aiortc, installed with Pipecat's WebRTC extra) that calls the
running app, sends a tone per call and checks each call hears its own tone back.

```sh
PROBE_TOKEN=<token, if one is set> uv run python scripts/echo_probe.py --url http://127.0.0.1:8000 --calls 2
```

`--relay` restricts the probe to TURN relay candidates.

## Run with Docker

```sh
cp .env.example .env   # set APP_ENV, the token and the TURN values
docker compose -f deploy/docker-compose.yml --env-file .env up --build
curl http://127.0.0.1:8000/health
```

The Compose file reads its values from the environment or the `--env-file`. The app port is
published on `127.0.0.1` only. coturn uses host networking (Linux hosts) and needs
`TURN_HOST` and `TURN_SHARED_SECRET`; it relays on UDP 49160-49200 and refuses to relay to
private, loopback and link-local addresses. coturn's own logging is switched off because it
records caller addresses.
