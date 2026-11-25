# Configuration rules

Part of the [README](../README.md)'s reference.

Settings come from the environment or `.env`. An invalid or missing value stops startup, and
an empty value counts as unset. Environment variables take precedence over `.env`. The same
`POSTGRES_*` values configure the database container in Compose, so there is one set of
database credentials.

`POSTGRES_PASSWORD` and `APP_DB_PASSWORD` follow the same rules: never empty, and unless
`APP_ENV=dev` at least 16 characters and not a placeholder from `.env.example`; startup stops
otherwise. In every environment the start-up steps refuse a `POSTGRES_PASSWORD` equal to
`APP_DB_PASSWORD` and a `POSTGRES_USER` equal to `APP_DB_USER`. The start-up service checks
every setting of the deployment, its own and the application's, together and as role `all`,
before it touches the database (`python -m app.config`); each running process checks its own
again when it starts.

Unless `APP_ENV=dev`, every secret (`POSTGRES_PASSWORD`, `APP_DB_PASSWORD`, `CALL_ACCESS_TOKEN`,
`TURN_SHARED_SECRET`, `AUDIT_HMAC_KEY`, `IDENTITY_HASH_KEY`, `ANTHROPIC_API_KEY`,
`DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`, `ADMIN_PASSWORD`) must also not start or end with whitespace and must
contain at least 10 different characters, on top of its own rules. coturn's entrypoint
refuses a `TURN_SHARED_SECRET` with fewer than 10 different characters too; it has no notion
of environment, so it applies the rule in dev as well.

`AUDIT_HMAC_KEY` must be set and not empty in every environment. Unless `APP_ENV=dev` it must
be at least 32 characters and must not be the placeholder from `.env.example`. Keep it outside
the database: whoever holds it can write events that verify. Changing it makes the existing
log fail verification.

`IDENTITY_HASH_KEY` follows the same rules as `AUDIT_HMAC_KEY` (its own placeholder). It keys
the hashes under which failed identity checks are counted across calls; choose one different
from the audit key. Changing it starts every count again.

`ADMIN_PASSWORD`, when set, needs `ADMIN_USER`, in every environment, and unless `APP_ENV=dev`
at least 16 characters. In every environment it must differ from every other secret: every
secret field of the settings, found from the class, so a secret added later is covered too. A
clash is reported by field name only. No long-running process holds every secret, so the
comparison that covers them all, the owner's `POSTGRES_PASSWORD` included, is the start-up
service's.

Each role refuses the secrets that are not its own, in every environment: `APP_ROLE=voice`
refuses `ADMIN_PASSWORD`; `APP_ROLE=admin` refuses `IDENTITY_HASH_KEY`, `TURN_SHARED_SECRET`,
`CALL_ACCESS_TOKEN` and the three vendor keys. The start-up check reads them all as role `all`,
and every secret there must differ from every other, the database passwords included.

`TURN_HOST` and `TURN_SHARED_SECRET` go together, and configuring TURN requires
`CALL_ACCESS_TOKEN` in every environment, so TURN credentials are never handed out without the
token. Generate the two secrets with `openssl rand -hex 32`.

`SIMULATED_NOW` fixes the clinic clock for dev and demo, for example to show the office
closed on a weekday afternoon. Empty means the real clock. Otherwise it must be an ISO 8601
date and time: `YYYY-MM-DDTHH:MM`, optionally with `:SS` and up to six decimals of a second,
`T` or a space between date and time, and optionally `Z` or an offset `+HH:MM` / `-HH:MM`.
Anything else, such as a bare number or a date alone, stops startup. A value without a zone,
such as `2026-11-24T10:30:00`, is clinic time (America/New_York); a wall time that a clock
change skips or repeats is refused, so give an offset for those. Startup stops if it is set
and `APP_ENV` is `prod`.
