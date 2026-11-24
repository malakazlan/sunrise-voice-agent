# Sunrise voice agent

A Python service that will become a voice agent. It is being built in small slices.

All data used by this project is synthetic. No real customer or personal data is stored or processed.

## What exists today

- `GET /health` returns `{"status": "ok"}`. It is a liveness check only.
- Configuration through environment variables or an optional `.env` file, validated at startup.
- Interactive API docs (`/docs`, `/redoc`, `/openapi.json`) are served only when `APP_ENV=dev`.
- A Postgres database with four clinic tables (`patients`, `medications`, `appointments`,
  `available_slots`) and the audit log (`audit_events`), created by Alembic migrations.
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
    time; `office_hours.clinic_now` gives the current or simulated time, in clinic time.
  - `matching.pick_patient`: which of the records already matched on date of birth and SSN
    last four the claimed name belongs to. Names are matched exactly, ignoring case, accents,
    spacing, hyphens, apostrophes and titles (`Dr`, `Mr`, `Mrs`, `Ms`, `Miss`): `Mary Ann
    Lee`, `Maryann Lee` and `Mary-Ann Lee` are the record Mary Ann / Lee. A suffix (`Jr`,
    `Sr`, `II`, `III`, `IV`) said or stored on one side only is ignored; on both sides it
    must be the same (`John Smith Sr` is not the record John / Smith Jr), and a name with two
    different suffixes is not a name. Any other difference, a middle name included, is a
    different name; there is no sound-alike or spelling-variant matching. Zero or two
    matching records is no match. `matching.name_key` is the name's letters with nothing
    between them and no suffix (`sarahjohnson`), so every spacing of a name has one key; it
    is coarser than matching (`Jo Anderson` and `Joan Derson` share a key).
  - `matching.match_medication`: which of the caller's own medications a spoken name means.
    It ignores case and form words (`tablet(s)`, `tab(s)`, `capsule(s)`, `cap(s)`,
    `pill(s)`, `inhaler`, `cream`, `ointment`, `solution`, `injection`, `patch`, `drops`,
    `spray`), so a form never tells two entries apart (`nicotine patch` finds a listed
    `Nicotine inhaler`). It also drops unit words (`mg`, `mcg`, `g`, `ml`, `milligram(s)`,
    `microgram(s)`, `gram(s)`, `unit(s)`, `iu`, `percent`, `%`) and a number, which may have
    one decimal point (`0.5`), when it is a strength: with a unit attached (`10mg`, `0.5mg`,
    `1%`) or right before a unit word (`10 mg`, `2000 IU`, `1 percent`). A strength never
    tells two entries apart either. A decimal comma is not understood. Every other number is
    kept and must match: `Humulin R U-500`, `Humulin 70/30` and `Vitamin B-12` keep theirs.
    A hyphen or slash next to a digit joins (`B-12` is `b12`, `70/30` is `7030`). After
    strengths are dropped, a single letter followed by a number is one word (`b 12` is
    `b12`, `u 500` is `u500`); a number followed by a unit word is a strength first, so
    `Vitamin C 500 mg` is `vitamin c` and `Humulin R U 500 units/mL` is `humulin r u`. The
    remaining words must match one for one, in order; a word of only letters, seven or more
    of them, may differ by one letter added, dropped, changed or swapped with its neighbour,
    never the first letter. A spoken name split in two (`met formin`) is joined when the
    listed name is one word and every spoken word has at least three letters and no digit.
    Zero or two matches is no match. Filler words (`my albuterol`) are not removed.

- An audit log, `app/audit/log.py`, the only code that writes or verifies the
  `audit_events` table (tool dispatch, below, is its first caller):
  - `AuditLog(key).append(session, event, now=...)` adds an event inside the caller's
    transaction and does not commit, so the event and the action it records commit or roll
    back together. Any failure, a cancelled task included, rolls back the caller's whole
    transaction. After its INSERT, `append` reads its row back and compares every column
    with what it wrote, so an INSERT that stores nothing, stores the row somewhere else, or
    stores it changed counts as a failure.
    Appends take a transaction-scoped advisory lock and wait for it at most 5 seconds (that
    timeout stays in force for the rest of the caller's transaction), so callers append
    last, just before they commit, at the top level of their transaction: an append in a
    nested transaction, or on a session in autocommit mode, is refused. `now` must carry a time
    zone. Callers use the default isolation level, READ COMMITTED; under REPEATABLE READ or
    SERIALIZABLE an append that meets another writer fails on the primary key (it fails
    closed).
  - Every database connection the app opens is ended by the server after 30 seconds idle
    inside a transaction, so an abandoned transaction cannot hold the lock for long. Opening
    a connection gives up after 5 seconds (`connect_timeout`) when the server does not answer,
    for the app and the start-up steps alike.
  - An event holds only registry codes (action, resource type, outcome, reason) and
    identifiers of a declared shape: a call id is a version-4 UUID in hex (its version and
    variant checked), a patient id is `P` and 3 to 6 digits, an appointment, medication or
    slot id is its row number (up to 8 digits), a tool id is its registered name (2 to 40
    lower-case letters and underscores), and the actor is `agent` or `admin:` and a
    user name of letters and underscores. The database's own CHECKs are the outer bound: at
    most 64 letters, digits, `_`, `-`, `:` and `.`, no spaces.
  - Each event carries `prev_mac` and `mac`: HMAC-SHA256 under `AUDIT_HMAC_KEY` over the
    previous MAC and the event's canonical form, which is documented in the module.
    `AuditLog(key).verify(session)` walks the whole chain in batches. Its result's
    `checked` is the number of events it read, good or broken; `breaks` lists every break
    (the first 100, and `truncated` says whether there were more): an edited event, a removed or
    duplicated one, a reordering, a forged event, the wrong key, or a stored value it cannot
    read. A row's content (its MAC) and its position (its seq) are both checked, so one row
    can carry two breaks, for example a gap and a MAC mismatch next to a removed row. After a
    break it goes on from that row's stored MAC, so later tampering is still found, and one
    edit can show twice: changing only a row's stored MAC breaks that row and the link of the
    row after it. It never raises because of what is stored. A row whose seq is null (only
    possible once the owner drops the primary key) is reported as unreadable; it sorts last,
    so no row comes after it to be checked against it.
  - Limits of the chain: removing the newest events is not visible from the chain alone;
    that needs the head kept outside the database. Anyone who can INSERT can add a row at
    the next position without the key; `verify` reports it as one break and checks the
    genuine events after it. The MAC does not bind a chain to one database, so every
    database gets its own `AUDIT_HMAC_KEY`: a chain copied from another database under the
    same key is not detected.
  - Limits of the identifier shapes: they say what an identifier looks like, not what it
    means. A patient id, a row id and the digits inside a call id can coincide with other
    numbers, such as part of a date, a phone number or an SSN, and an admin user name can be
    a person's name. The control for `call_id` is where it comes from: the server generates
    it and never takes one from the caller or the model.
  - What the database enforces: triggers refuse `UPDATE`, `DELETE` and `TRUNCATE` on
    `audit_events`, and any `INSERT` out of chain position, for ordinary statements from
    every role, superusers included. Whoever owns the table, or a superuser, can still
    disable or drop the triggers or drop the table; the chain still shows any edit made that
    way. The repo's scans that keep writes to the table in `app/audit/`, the trigger switch in
    the test fixture, and raw `SAVEPOINT` or `ROLLBACK TO` text out of `app/` are lints
    against accidents, not controls: the savepoint scan reads literal strings only, so it does
    not see an SQL comment between `ROLLBACK` and `TO`, `ROLLBACK WORK TO` or
    `ROLLBACK TRANSACTION TO`, or text assembled at runtime. The control is the runtime
    database role below: the app connects as a role that neither owns the table nor is a
    superuser, and has no `UPDATE`, `DELETE` or `TRUNCATE` on it.

- Two database identities, and who holds what:
  - The owner (`POSTGRES_USER`, `POSTGRES_PASSWORD`, the superuser the Postgres image
    creates) is used only by the start-up steps: settings check, migrations, role set-up and
    the seed. They run in `startup`, a one-shot Compose service from the same image, which
    exits when they are done, and they read `StartupSettings` (`app/config.py`), the only
    settings class with the owner's fields.
  - The `app` service starts only once `startup` has exited with 0. Its container is given
    the application's settings only: the runtime role (`APP_DB_USER`, `APP_DB_PASSWORD`),
    where the database is (host, port, name) and the rest of `Settings`, which has no owner
    field at all. So no process in it, the server and the image's `HEALTHCHECK` included,
    ever has the owner's identity. CI checks each service's configured environment against
    its settings class, scans every process in the running app container while a
    `HEALTHCHECK` runs, and tries every value found there as the owner's password.
  - One `.env` file can hold both: each settings class takes its own keys from it and leaves
    the other's, and any key that belongs to neither still stops start-up.
  - `python -m app.db.roles` sets up the runtime role as the owner on every start of the
    stack, in one transaction under an advisory lock of its own, so set-ups that run at once
    take turns:
  - It is created if missing, with `LOGIN` and nothing else: not a superuser, cannot create
    roles or databases, no replication, no bypassing row security.
  - Its password is set on every start, so a changed `APP_DB_PASSWORD` works after the next
    start and the old one stops working for new logins. The password is never sent to the
    server: libpq (through psycopg) turns it into a SCRAM-SHA-256 verifier on the app's side,
    and only that goes into the `ALTER ROLE` statement, which is sent straight through the
    driver, so no log line, SQLAlchemy event or error message contains it, at any log level.
    A verifier in any other form is refused before it is sent.
  - It may do exactly what `app/db/grants.py` declares, and nothing else: `SELECT`, `INSERT`,
    `UPDATE` on `patients` and `appointments`; `SELECT` on `medications`; `SELECT`, `UPDATE`
    on `available_slots`; `SELECT`, `INSERT` on `audit_events`; nothing on `alembic_version`;
    `USAGE` on the appointment id sequence only; no `EXECUTE` on the trigger functions (a
    trigger runs its function whoever may call it) or on the functions that create large
    objects; `USAGE` on the schema; `CONNECT` on the application's database and nothing on any
    other database of the server. No table allows it `DELETE` or `TRUNCATE`. It cannot create
    a table, function, trigger, role or schema in the application's database, nor a temporary
    table or function there, and it cannot connect to any database that existed at set-up time.
  - Anything else it holds is taken away: grants the owner made it in this database and on
    databases, memberships the owner granted it (one another role granted is left in place,
    and the start refuses it), settings attached to the role, parameter privileges,
    and what it can make itself own and Postgres cannot forbid: default privileges, and large
    objects (one may remain from before their creation was closed). `PUBLIC` loses every
    privilege on the tables, sequences and functions, `CREATE` on the schema, `CREATE` and
    `TEMPORARY` on this database and everything on every other database.
  - Then set-up compares what the role can effectively do (through its own grants, `PUBLIC`'s
    and any role it belongs to) and what it is (attributes, memberships, settings, what it
    owns) with the declaration, and stops the start on any difference, listed by kind and
    name. It reads every privilege list Postgres keeps, system objects included: relations
    and their columns, functions, schemas, types, languages, foreign-data wrappers and
    servers, large objects, tablespaces, parameters, databases and default privileges. Any
    entry there naming the runtime role must be one the declaration makes; `PUBLIC`'s entries
    on everything the declaration does not manage must be exactly those Postgres installed
    (`pg_init_privs`, and for `information_schema`, which initdb does not record there,
    `USAGE` on the schema and `SELECT` on its relations but seven); no role may belong to the
    runtime role, nor it to any. A grant another role made with a grant option, which the owner
    cannot revoke, stops it that way; so does a table no declaration covers. If the role owns
    anything else, such as a table, function or schema, set-up stops and names the kind rather
    than drop it with its contents.
  - Every session the start-up steps open (migrations, role set-up, seed) waits at most 30
    seconds for any one lock and runs no statement longer than 5 minutes; the steps take about
    one to two seconds each. A step that hits either limit stops the start with a message saying
    what it waited for. No session of another role is ended.
  - The audit triggers' functions run with a pinned search path and name their table with its
    schema, so nothing created later can stand in for what they use.
  - What remains:
    - The owner's password is in the `startup` service's configuration (and in the `.env`
      file). Whoever controls the Docker host (`docker inspect`, `docker compose config`) can
      read it.
    - The verifier is in the text of the `ALTER ROLE` statement the server receives. Postgres
      would log it if `log_statement` were set to `ddl` or `all` (Compose keeps statement
      logging off). A verifier alone does not log in, but whoever holds it can guess the
      password offline, can pose as the server to a client of this role, and with one recorded
      login exchange of the role can log in as it. The password rules below still matter.
    - Anyone who can read the app container's environment gets the runtime password, which
      allows exactly the privileges above and nothing more.
    - The role can change its own password and its own session defaults (`ALTER ROLE` on
      itself, `ALTER DEFAULT PRIVILEGES`); the next start-up restores both.
    - The role must be able to take the audit chain's advisory lock, so a hostile session of
      the role can hold it and make every audit append time out (after 5 seconds) until that
      session ends.
    - The role can end its own sessions (`pg_terminate_backend` on a backend of the same role).
    - A rotated password does not end sessions opened with the old one.
    - A hostile session of the runtime role can make start-up fail, by holding the role
      set-up's advisory lock or a lock on a table it may write, until that session ends.
      Restarting the database ends it.
    - Use a dedicated database server. Do not point `POSTGRES_*` at a server that hosts other
      applications: set-up takes `PUBLIC`'s privileges away on every other database on that
      server, which locks those applications out, and `DROP OWNED` takes grants on databases
      away server-wide. The Compose `db` service is such a dedicated server.
    - Do not create other databases on this server. A database created after a start-up gives
      everyone `CONNECT` and `TEMPORARY` by default, so the runtime role can connect to it and
      create objects there before the next start-up; that start-up then refuses to run until
      those objects are removed by hand.

- Masked log output. `app/guard/logging.py` is the only place log output is configured; the
  application and each start-up step call it first. Every line goes to stderr as
  `timestamp LEVEL logger message`, and the message and any traceback pass through
  `app/guard/mask.py` first, line by line. A record's `extra` fields are not printed.
  - SSNs (`***-**-****`; after a cue such as `ssn`, `social`, `last four`, `ssn_last4`,
    `ending in`, the digits or number words that follow become `****`), dates
    (`****-**-DD`, Python's `datetime.date(...)` included), phone numbers (`***-***-` and
    the last four digits) and email addresses (`s***@***.com`) are masked. A line longer than
    10,000 characters is cut, with ` [cut]` at the end, before masking, so the last line of a
    long traceback stays. Call ids, patient ids, times, amounts, years, version numbers and
    four-digit numbers without a cue are left alone; a nine-digit or ten-digit number on its
    own is masked whatever it is.
  - Python's warnings, exceptions nobody catches (in the main thread or another) and records
    that find no handler are logged through the same formatter.
  - The level is `LOG_LEVEL`, for the root logger and uvicorn's loggers. uvicorn's access log
    is off. Pipecat (through loguru) and its WebRTC stack (`aioice`, `aiortc`) keep only
    warnings and errors, and loguru does not print the values of local variables.
  - Masking free text is best effort. The controls that matter are that the app logs event
    names, ids and counts, and that the input guard swallows SSN digits before anything
    could log them.
  - Not handled: digits said before the cue (`3344 is my ssn`), seven-digit local numbers,
    international numbers, eight-digit compact dates, quoted or internationalized email
    addresses, an identifier split across two log calls, and names. Pipecat prints a version
    banner through loguru's own sink when it is imported, before the configuration runs; a
    handler a library adds after the configuration is not covered. The repo's scans that
    keep log configuration in one module are lints against accidents.

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

### Call state and the tool gate

No real tool is registered yet; later slices add the tools and wire them to the model.

- `app/agent/state.py`: a call's state (`CallState`, immutable) and its phases: `greeting`,
  `intake`, `serving`, `booking`, `emergency`, `handoff`, `closing`, `ended`. A call starts
  with `new_call` (greeting, unverified, nothing counted, a call id from the call registry);
  nothing else in `app/` constructs a state. A state changes only through transition
  functions, which refuse illegal moves (`IllegalTransitionError`): greeting to intake;
  intake to booking (a new patient); serving to booking, and booking to serving for a
  verified caller only (a new patient's booking never reaches serving); intake, serving or
  booking to closing, or to handoff while the office is open; any phase but ended to
  emergency; any phase but ended to ended (the caller can hang up at any time). Ended is
  final. `verify` is the only move from intake to serving and the only code that sets
  `verified` and the patient id, and it refuses while the office is closed. Every state is
  validated for consistency: verified goes with a patient id and a first name, an open office
  and a phase a verified caller can be in; counters are never negative. These rules hold
  against the model and the caller; code that deliberately bypasses validation
  (`model_construct`, `object.__setattr__`) can still forge a state.
- `app/agent/tool_registry.py`: a tool is declared once (name, argument model, handler,
  whether it touches patient data, its phases, whether it needs the office open, whether it
  writes, whether it may verify the caller, its audit action and resource type). Registration
  refuses a declaration that breaks a rule: every field given and of the right kind, the
  audit action from the audit registry and not one dispatch or the call writes itself
  (`tool_refused`, `tool_failed`, `call_started`, `call_ended`); no argument naming a patient
  or a record owner (`patient`, `patient_id`, `ssn`, `dob` and the like), by field name,
  alias or any validation alias (each choice, each step of a path), in nested models too;
  every argument model, nested ones included, forbids unknown fields; no argument takes free
  keys (a mapping), `Any` or `object`; a patient-data tool only in `serving` and `booking`; a
  writing tool's arguments have `confirmed: bool = False`; a tool that may verify is not a
  patient-data tool, is in `intake` only, and there is at most one; the name is 2 to 40
  lower-case letters and underscores.
- `app/agent/tools.py`: `register_tools()` is the one entry point that registers every tool
  on the application's registry and seals it; after that, registering refuses. The app calls
  it at start. The structural tests call it after importing every module under `app/`, so a
  tool registered anywhere is checked against the gate's rules; the registry is constructed
  once in `app/`.
- The gate (`tools_for`): no tool at all in `greeting`, `emergency`, `handoff`, `closing` or
  `ended`; a tool only in its declared phases; an office-hours tool only while the office is
  open; a patient-data tool only for a verified caller with a patient id, while the office is
  open. Since such a tool can only be declared for `serving` and `booking`, it is never
  offered anywhere else.
- `dispatch` is the only caller of a handler. It refuses a tool name that is not a registered
  one (or not text), a tool the gate does not allow, arguments larger than 16 KiB as JSON (or
  not JSON at all) and arguments that do not validate, without running the handler.
  Otherwise, in one transaction and within 10 seconds, the handler runs (it gets the patient
  id from the state, never from the arguments), the transition it asks for is applied (a
  `Verify` only from the tool declared to verify), the transaction is checked to be the one
  the handler was given, the audit event is appended, and it commits. The check reads the
  transaction's id (`pg_current_xact_id`, unique for the life of the server) before the
  handler and again after, so a handler that commits or rolls back, with SQLAlchemy or with
  SQL text, fails the call; a commit through SQLAlchemy is refused before it reaches the
  database. On any failure the connection is closed rather than reused, so the server rolls
  back whatever it holds. A handler that runs a raw `COMMIT` statement has committed by the
  time dispatch regains control: the call fails, and no success event is written, but the
  rows it committed stay; the repo's scan keeps such SQL text out of `app/`.
- Every dispatch leaves exactly one audit event. A tool call's event names the tool when its
  resource type is `tool`, the caller's patient id (from the state, never from the handler)
  when it is `patient`, and otherwise the record the handler names. `tool_refused` (outcome
  `refused`) and `tool_failed` (outcome `failure`) events have resource type `tool` and the
  tool's name as id (none for a name that is not a registered tool) and are written in their
  own transaction. When the call is cancelled mid-tool (the caller hangs up), the work is
  rolled back and no event is written for that tool call; the call's own end event records
  the hang-up.
- The model gets fixed text for a refusal or a failure, never an exception or an argument
  value. Failures and refusals are counted separately: from two failures, or five refusals,
  every outcome asks for a hand-off. Tool calls of one call run one at a time.
- The application's database sessions end any statement after 5 seconds, so a connected but
  stalled server cannot hold a tool call; the start-up steps keep their own limits.
- The repo's scans that keep handler calls inside `dispatch` (any `.handler` reference, the
  text `"handler"` as `getattr` takes it, a registered handler called by name) and raw
  transaction statements out of `app/` are lints against accidents, not controls: they read
  the source as written.

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
| `APP_DB_USER`       | runtime role: `a-z 0-9 _`, up to 63, not `pg_...`, not `POSTGRES_USER` | none, required |
| `APP_DB_PASSWORD`   | runtime role's password, never logged, not `POSTGRES_PASSWORD` | none, required |
| `AUDIT_HMAC_KEY`    | audit chain key, never logged       | none, required |
| `MAX_CONCURRENT_CALLS` | 1 to 20, digits only             | `3`            |
| `MAX_CALL_SECONDS`  | 1 to 3600                           | `600`          |
| `TURN_HOST`         | host name, no scheme or port        | required unless dev |
| `TURN_SHARED_SECRET`| 32+ of `A-Z a-z 0-9 . _ ~ + / = -`, never logged | required unless dev |
| `DEMO_ACCESS_TOKEN` | at least 32 characters, never logged | required unless dev |

Settings come from the environment or `.env`. An invalid or missing value stops startup, and
an empty value counts as unset. Environment variables take precedence over `.env`. The same
`POSTGRES_*` values configure the database container in Compose, so there is one set of
database credentials.

`POSTGRES_PASSWORD` and `APP_DB_PASSWORD` follow the same rules: never empty, and unless
`APP_ENV=dev` at least 16 characters and not a placeholder from `.env.example`; startup stops
otherwise. In every environment the start-up steps refuse a `POSTGRES_PASSWORD` equal to
`APP_DB_PASSWORD` and a `POSTGRES_USER` equal to `APP_DB_USER`. The start-up service checks
its settings before it touches the database (`python -m app.config`); the app checks its own
when it starts.

Unless `APP_ENV=dev`, every secret (`POSTGRES_PASSWORD`, `APP_DB_PASSWORD`, `DEMO_ACCESS_TOKEN`,
`TURN_SHARED_SECRET`, `AUDIT_HMAC_KEY`) must also not start or end with whitespace and must
contain at least 10 different characters, on top of its own rules. coturn's entrypoint
refuses a `TURN_SHARED_SECRET` with fewer than 10 different characters too; it has no notion
of environment, so it applies the rule in dev as well.

`AUDIT_HMAC_KEY` must be set and not empty in every environment. Unless `APP_ENV=dev` it must
be at least 32 characters and must not be the placeholder from `.env.example`. Keep it outside
the database: whoever holds it can write events that verify. Changing it makes the existing
log fail verification.

`TURN_HOST` and `TURN_SHARED_SECRET` go together, and configuring TURN requires
`DEMO_ACCESS_TOKEN` in every environment, so TURN credentials are never handed out without the
token. Generate the two secrets with `openssl rand -hex 32`.

`SIMULATED_NOW` fixes the clinic clock for dev and demo, for example to show the office
closed on a weekday afternoon. Empty means the real clock. Otherwise it must be an ISO 8601
date and time: `YYYY-MM-DDTHH:MM`, optionally with `:SS` and up to six decimals of a second,
`T` or a space between date and time, and optionally `Z` or an offset `+HH:MM` / `-HH:MM`.
Anything else, such as a bare number or a date alone, stops startup. A value without a zone,
such as `2026-11-24T10:30:00`, is clinic time (America/New_York); a wall time that a clock
change skips or repeats is refused, so give an offset for those. Startup stops if it is set
and `APP_ENV` is `prod`.

## Run with Docker

```sh
cp .env.example .env          # then choose your own POSTGRES_PASSWORD and APP_DB_PASSWORD
docker compose --env-file .env -f deploy/docker-compose.yml up --build
curl http://127.0.0.1:8000/health
```

Four services start: `db`; `startup`, a one-shot run from the app's image that checks its
settings (`python -m app.config`), then, as the owner, runs `alembic upgrade head`, the role
set-up (`python -m app.db.roles`) and the seed, and exits; `app`, the server, which starts only
after `startup` has exited with 0 and connects as the runtime role; and `coturn`. A failure in
any start-up step ends `startup` with a non-zero code and `app` is not started (`docker
compose up` reports it and exits non-zero). `startup` shows as exited once it is done; that is
its normal state. The database is not published to the host.

Restarts: `docker compose up -d` after `stop` runs `startup` again before `app`; on a running
stack it runs `startup` again and recreates `app` only if its configuration
changed, as after a password rotation. Restarting only the app (`docker compose restart app`)
does not need `startup`.
`docker compose restart` with no service restarts `startup` and `app` together without waiting
for `startup` to finish, so use `up -d` to restart the stack.

`--env-file .env` is required: Compose takes every value from it and refuses to start, naming
the variable, if `APP_ENV` or a database credential is missing. Each container receives only
its own variables, as follows.

| Service   | Variables |
|-----------|-----------|
| `db`      | `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` |
| `startup` | `APP_ENV`, `LOG_LEVEL`, `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `APP_DB_USER`, `APP_DB_PASSWORD` |
| `app`     | `APP_ENV`, `LOG_LEVEL`, `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `APP_DB_USER`, `APP_DB_PASSWORD`, `AUDIT_HMAC_KEY`, `SIMULATED_NOW`, `MAX_CONCURRENT_CALLS`, `MAX_CALL_SECONDS`, `TURN_HOST`, `TURN_SHARED_SECRET`, `DEMO_ACCESS_TOKEN` |
| `coturn`  | `TURN_HOST`, `TURN_SHARED_SECRET`, `MAX_CONCURRENT_CALLS` |

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
`.env`, whose unknown keys the app refuses.

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
`.env`. The user must be a superuser: each test gets its own database, migrated with Alembic,
and drops it afterwards, and the audit tamper tests switch the audit table's triggers off for
one transaction to show that the chain catches edits. The tests use their own audit key.
Each test run also creates its own runtime role (`sva_test_<random>_app`, a random password)
and sets it up in every test database, so tests of the application run as that role; set-up,
schema checks, the seed and the tamper fixture use the owner. Roles belong to the whole
server, so every run and every role test uses a name of its own, and the role is dropped when
the run ends; one left behind by a crashed run never meets another run.
