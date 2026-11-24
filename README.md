# Sunrise voice agent

A Python service that will become a voice agent. It is being built in small slices.

All data used by this project is synthetic. No real customer or personal data is stored or processed.

## What exists today

- `GET /health` returns `{"status": "ok"}`. It is a liveness check only.
- Configuration through environment variables or an optional `.env` file, validated at startup.
- Interactive API docs (`/docs`, `/redoc`, `/openapi.json`) are served only when `APP_ENV=dev`.
- A Postgres database with four clinic tables (`patients`, `medications`, `appointments`,
  `available_slots`), what callers ask for (`requests`, `messages`) and the audit log
  (`audit_events`), created by Alembic migrations.
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
    variant checked), a patient id is `P` and 3 to 6 digits, an appointment, medication,
    slot, request, message or audit event id is its row number (up to 8 digits), a tool id is its registered name (2 to 40
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
    on `available_slots`; `SELECT`, `INSERT` on `audit_events`, `requests` and `messages`;
    nothing on `alembic_version`; `USAGE` on the appointment, request and message id
    sequences only; no `EXECUTE` on the trigger functions (a
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

### Voice call: speech in, the model and its tools, text out

A call takes the caller's audio over WebRTC (Pipecat's SmallWebRTC transport) through voice
activity detection (Silero) and Deepgram's streaming speech-to-text to the agent, and sends
each turn's result to the call page over the data channel. Nothing is spoken back yet.

- `GET /` serves a plain test page: access token, Call and Hang up, a "relay only" switch, the
  connection state, the ICE candidate types in use, and one entry per turn: what the model was
  given (the guard's text, or "(not shown to the assistant)" for a swallowed or emergency
  turn), the lines said, the timings and whether a hand-off was asked for. The server's text
  is written as text, never as markup.
- `GET /ice-servers` returns the ICE server list with short-lived TURN credentials (coturn's
  shared-secret scheme; they expire `MAX_CALL_SECONDS` plus 60 seconds after they are issued).
- `POST /offer` takes an SDP offer (a JSON body of at most 32 KiB) and returns the answer.
- Both need `Authorization: Bearer <DEMO_ACCESS_TOKEN>`. The token is checked before any of
  the body is read. When no token is configured (only allowed in dev without TURN) the two
  endpoints are open.
- At most `MAX_CONCURRENT_CALLS` calls at once (more get 503). A call ends when the caller
  leaves, when it has not connected within 30 seconds, after `MAX_CALL_SECONDS`, when
  speech-to-text cannot connect, or when a turn fails in a way the agent does not handle.
  Without both vendor keys (allowed in dev only) a call is refused at set-up.
- Speech-to-text (`app/voice/pipeline.py`): Deepgram `nova-3-general`, `en-US`, punctuation
  and smart formatting on, numerals off (smart formatting already writes numbers as numerals),
  opted out of Deepgram's model improvement programme. Smart formatting writes "three three
  four four" as `3344` and a date as `March 14, 1985`; a spelled name stays letters (`J O H N
  S O N`); the input guard reads digits and number words either way. The caller is taken to
  have stopped after 0.8 seconds of silence; Deepgram is then asked to finalize, and only a
  finalized transcript makes a turn.
- The turn (`app/agent/turn.py`, no Pipecat in it): the input guard decides first. Emergency:
  the emergency line, the call moves to `emergency`, no model request. Swallowed (the SSN
  step): the model sees nothing of the turn; when the guard says submit, code runs
  `submit_claim` and its line is the reply. Pass: a submit the guard asks for runs first, then
  one model turn with the guard's text. Turns run one at a time.
- A model turn sends the system prompt (`app/agent/prompts.py`: fixed text, no patient data,
  written for speech), the conversation so far and the tools `tools_for` allows right now,
  each with its argument model's JSON schema and a fixed description. Every tool call goes
  through `dispatch`, and its outcome goes back to the model, unless it carries a scripted
  line: that line is the reply, word for word, the model is told it was said, and tools asked
  for after it in the same reply do not run. Scripted lines join the conversation as the
  model's own; swallowed turns never join it. At most four tool rounds per turn.
- The model (`app/agent/model.py`): Claude through the official Anthropic SDK, async and
  streamed, `LLM_MODEL` (default `claude-haiku-5-5`), `max_tokens` 300, effort `low`, no
  `temperature` or other sampling parameter, no SDK retries. A refusal, a failed request, no
  first token within 8 seconds, a reply not finished 20 seconds after that, a reply that is
  neither an answer nor a tool request (cut off at `max_tokens`, say), or a fifth tool round:
  the fixed fallback line, and the call goes on. Two in one call, or a dispatch outcome that
  asks for one, raise the hand-off signal. Thinking is off in every request (`thinking:
  disabled`, which Claude Haiku 5.5 accepts at effort `low`): on a phone call it would delay
  the first word and use up the 300 tokens; a `LLM_MODEL` that refuses thinking off (Claude
  Opus 5.5 and Sonnet 5.5 do) gets the fallback line on every turn.
- `Deps` (sessions as the runtime role, the audit log, the clinic clock with `SIMULATED_NOW`,
  the claim hasher) is built once at start-up from the settings (`build_deps` in
  `app/main.py`, kept on `app.state.deps`).
- Per turn, logged as numbers only (`turn_measured`) and shown on the page: speech end to
  final transcript, final transcript to the model's first and last token, tool rounds, and
  speech end to the reply sent. The vendor clients' loggers (`anthropic`, `httpx2`, `httpx`,
  `httpcore`, `websockets`, `deepgram`) keep only warnings and errors: below that they write
  request bodies and websocket frames.
- coturn runs as a Compose service built from `deploy/coturn`: one configuration file with
  allocation and bandwidth quotas, no relaying to private, loopback, link-local or unspecified
  IPv4 addresses, and its own logging switched off because it records caller addresses.
  Every TURN credential has its own username (`<expiry>:<random id>`), so coturn's per-user
  quota (4 allocations, one call's worth) limits each credential; the total quota is 9 per
  allowed call, and an allocation nobody refreshes ends within 120 seconds. The unspecified
  IPv6 address is not denied: coturn 4.19.2 matches that rule against IPv4 peers as well and
  then refuses every relayed call.

Limits of this slice:

- No voice out yet: replies are text on the page.
- No output guard yet: the model's text reaches the page unchecked. All data is synthetic.
- Booking, messages, transfer and call end are not wired; the hand-off signal is shown and
  nothing acts on it. The guard's flags (an attempt to override the instructions, a request
  about someone else) are not passed to the model.
- One call's conversation is kept in memory only, for the call's life.
- The verified line, said by code, puts the caller's first name in the conversation the model
  sees; tool results are the only other patient data it receives.
- Tests and CI never reach a vendor: the model is scripted, speech-to-text is replaced, and
  CI's forced-relay calls run with the vendor hosts pointed at the app's own loopback. The
  proof with the vendors is a real call on the server.

What has been run: the test suite on Windows 10. `scripts/relay_probe.py` (an aiortc client
that sends a tone and reads the server's receiver reports for it, since nothing comes back)
runs in CI against the app forced through coturn, with only the networking changed
(`deploy/docker-compose.relay-test.yml`: app and coturn on 203.0.113.0/24, which coturn does
not deny, and the vendor hosts on the app's loopback).

Generated slots: on every run the seed also makes sure the next 14 business days (Monday to
Friday, strictly after today in clinic time, no holiday calendar) have slots for Dr. Patel at
9:30 AM, 11:00 AM and 3:00 PM and for Dr. Lee at 10:00 AM and 1:30 PM, America/New_York time.
These rows have `source = 'generated'`; the slots from the task data have `source = 'task'`.
A generated slot never replaces an existing slot at the same doctor and time.

### Call state and the tool gate

The tools registered so far are the two of identity verification and the five serving tools
(below); a call's turns (above) offer them to the model through the gate.

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
  writes, whether the model may call it, whether it may verify the caller, whether it takes
  an identity claim, its audit action, any other actions its events may carry with each one's
  fixed outcome, and its resource type). Registration
  refuses a declaration that breaks a rule: every field given and of the right kind, the
  audit action from the audit registry and not one dispatch or the call writes itself
  (`tool_refused`, `tool_failed`, `call_started`, `call_ended`); no argument naming a patient
  or a record owner (`patient`, `patient_id`, `ssn`, `dob` and the like), by field name,
  alias or any validation alias (each choice, each step of a path), in nested models too;
  every argument model, nested ones included, forbids unknown fields; no argument takes free
  keys (a mapping), `Any` or `object`; a patient-data tool only in `serving` and `booking`; a
  writing tool the model can call has `confirmed: bool = False` among its arguments (a
  code-only tool is never called on the model's word); a tool that may verify is not a
  patient-data tool, is in `intake` only, is code-only, and there is at most one; a tool that
  takes an identity claim is not a patient-data tool, is in `intake` only with the office
  open, is callable by the model, may not verify, and there is at most one: it alone may have
  a `dob` argument; a code-only tool is registered with a grant of its own (`Grant`), a
  model-callable one without; the name is 2 to 40 lower-case letters and underscores.
- `app/agent/tools.py`: `register_tools()` is the one entry point that registers every tool
  on the application's registry and seals it; after that, registering refuses. The app calls
  it at start. The structural tests call it after importing every module under `app/`, so a
  tool registered anywhere is checked against the gate's rules; the registry is constructed
  once in `app/`.
- Who calls: `tools_for` offers the model only model-callable tools. `dispatch` without a
  grant is a model call and refuses a code-only tool; with a grant it is a code call and
  refuses any tool that grant was not registered with. The grant is an object held by the code
  that calls the tool, never anything the model sends. The gate below applies to both.
- The gate: no tool at all in `greeting`, `emergency`, `handoff`, `closing` or
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
  back whatever it holds.
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
- Limit: a handler that runs a raw `COMMIT` statement has committed its work before dispatch
  regains control, and dispatch cannot undo it. The call fails and no success event is
  written, but the committed rows stay; the scan keeps such SQL text out of `app/`.

### Identity verification

Before any patient information, the caller gives a full name, a date of birth and the last
four digits of their SSN; all three must match one record, and the digits never reach the
model. Code decides; the model only relays the name and the date.

- `app/guard/input.py`: `guard_input(text, state)` decides each caller turn, purely (no
  database, network or clock), in this order. **Emergency**: a phrase of the emergency list
  (chest pain, can't breathe, suicid..., overdose, stroke, heart attack, passed out, severe
  bleeding and the others), negated or not, wins in every phase: the model sees nothing of
  the turn, code speaks the `emergency` line and the call moves to `emergency`. **The SSN
  step**, while armed: a turn with any digit or number word (numerals in any script, zero to
  nine, the teens, the tens, hundred, thousand, double, triple, and the homophones won, to,
  too, tree, for, fore, ate when they stand next to a digit or each other) is swallowed whole
  and its digits are collected; a turn without one passes. Number words are read where that
  is unambiguous: `nineteen thirty` is 1930, `thirty three forty four` is 3344, `sixteen
  hundred` is 1600. Four digits in all submit them; more than four clear the buffer and ask
  again (`ssn_too_long`); the third armed turn without four submits what there is.
  **Redaction**, always: digits after an SSN cue, SSN-shaped numbers, and any run of digits
  or number words that reads as exactly nine digits, whatever its grouping (spaces, hyphens,
  dots, the word "dash"). In `greeting` and `intake`, before verification, every other digit
  and number word is hidden too, except inside a date the mask module recognises, a year from
  1900 to 2099 (numerals or words) or a run of ten or eleven digits (a callback number). Once
  verified, any group that reads as the caller's own last four is hidden; a phone number that
  ends in those digits is not. **Flags**: an attempt to override the instructions, and a
  request about someone else; the text still passes. The decision carries the state change as
  a transition; its caller applies it with `apply`.
- `app/guard/mask.py` stays the one owner of the SSN patterns: the guard uses its cue and SSN
  alternatives and its nine-digit rule (`mask_ssns`, which `mask()` for the logs and the
  doctor-message tool use too), its allow-list for unverified callers
  (`mask_unverified_digits`), its last-four rule (`mask_known_digits`) and its digit reading
  (`spoken_digits`, `says_a_number`). A repo scan refuses SSN patterns or lists of digit words
  anywhere else under `app/`; it is a lint, like the others.
- `verify_identity(full_name, dob)`: the model calls it in `intake` with the office open.
  The date must be an ISO date (`YYYY-MM-DD`) not after the clinic's date, and the name must
  be a name; otherwise the arguments are refused and nothing is counted. It stores the claim,
  arms the SSN step and returns `ssn_ask`. If the call is locked, or the claimed identity has
  five failures within the last hour across calls, it returns `verify_locked`, arms nothing
  and ends any claim the call already held. It never reads a patient record.
- `submit_identity()`: code-only; `submit_claim` in `app/agent/tools.py` calls it when the
  guard says submit. It reads the claim and the digits from the state, selects the records
  with exactly that date of birth and those four digits, and `pick_patient` must find exactly
  one by name. A match verifies the caller (`verified`, with their first name) and keeps the
  digits as `verified_ssn4`. Anything else is one failed attempt, identical whatever did not
  match: the same `verify_fail` line, the same `identity_refused` event, the same state
  change; the third locks the call (`verify_locked`, `identity_locked`). Every submit is
  recorded in `verification_attempts`, in one transaction with the state change and the
  audit event. Before anything else that transaction takes an advisory lock on the claim's
  key and counts its failures again: calls armed at the same moment submit one after another,
  and once the identity has five failures a submit returns `verify_locked` and counts
  nothing, so no more than five failures are recorded in the hour. A submit that does not run to an outcome (the database is down) counts
  nothing and verifies nothing, and `submit_claim` drops the claim and the digits.
- `verification_attempts` holds the claim's keyed hash (HMAC-SHA256 under
  `IDENTITY_HASH_KEY` over the name's letters and the ISO date), the call id, whether it
  matched and when; never a name, a date or a digit. The runtime role may only read and
  insert. A success does not clear earlier failures; failures of claims that match nobody
  count the same, so a lock says nothing about who exists.
- The claim and the SSN step live in `intake` with the office open; any phase change ends
  them, and a second claim replaces the first. `pending_ssn4` and `verified_ssn4` are excluded
  from every dump and from the state's text form by their declaration; validation errors do
  not show input. Events carry `identity_claimed`, `identity_verified` (the patient id),
  `identity_refused` and `identity_locked`, with no factor, name or digit. The scripted lines
  are in `app/agent/scripts.py`.

Limits, stated plainly:

- A last four that reads as a year (1900 to 2099), said before the step is armed and
  without a cue, reaches the model. The cost of the allow-list: before verification, the
  model does not see other small numbers either ("I have 2 questions", a time such as 10:30);
  only dates, years and callback numbers pass. In other phases before verification (a new
  patient's booking), numbers other than SSNs pass as said.
- Spoken number words count as digits while the step is armed: "one moment" adds a 1, and a
  turn with "double" or "a thousand" is swallowed though it adds nothing. "oh" is a zero only
  next to another digit; the homophones are digits only next to a digit or each other ("for
  to for to"), so "can I talk to a person" passes. The "four" of "last four" is not a digit.
- Anyone who knows a patient's name and date of birth can lock that patient out of phone
  verification for an hour with five failed calls; a transfer to staff remains open.
- On the third armed turn without digits the decision both submits and passes the text: the
  caller of the guard runs the submit first, then gives the model the text. A third turn with
  more than four digits submits an empty buffer.
- An emergency while armed: the model sees nothing of the turn; the move to `emergency` ends
  the step.
- `pydantic`'s `errors()` returns a rejected value when asked for its input; the error's text
  form does not. No application code reads `errors()` of a state.
### Serving tools

Five tools for a verified caller, in `app/agent/serving.py`, registered by `register_tools()`.
Each is a patient-data tool offered only in `serving`, with the office open, and acts only on
the state's patient id; none takes a patient identifier. Dispatch audits every call with
resource type `patient`, the caller's patient id and an action that says what happened:
`appointment_read`, `lab_status_read`, `balance_read`; for a refill `refill_requested` (the
request row written, or already there for this call), `refill_previewed` (needs confirmation)
or `refill_not_found` (outcome `refused`); for a message `doctor_message_sent` (written, or
already there) or `doctor_message_previewed`. Every other outcome is `success`. No medication
name, message text, balance or status reaches the audit log or the logs.

- `get_next_appointment`: the caller's earliest `scheduled` appointment at or after the moment
  of the question on the call's clock, as
  `Thursday, December 3 at 10:30 AM with Dr. Patel` in clinic time, or none.
- `request_refill(medication_name, confirmed)`: the name is matched with `match_medication`
  against the caller's own medications only. No match, or more than one: `not_found` and
  nothing else, no list. A match without `confirmed: true`: `needs_confirmation` with the
  medication and pharmacy to read back. Confirmed: a `requests` row (type `refill`, summary
  `<medication> at <pharmacy>`, built by code and cut to its first 200 characters) and `requested`, or `sent_to_doctor` when no
  refills are left, with the medication and pharmacy. The refill count is never returned.
- `get_lab_status`: the stored status text only; the clinic stores no results.
- `get_balance`: the balance as an exact two-decimal string (`45.00`).
- `send_message_to_doctor(summary_short, confirmed)`: 1 to 120 characters after trimming, with
  no control character (C0, NUL included, or DEL) inside; anything else is refused without
  echoing it. Without `confirmed: true`:
  `needs_confirmation` with the text as it will be stored. Confirmed: a `messages` row (kind
  `doctor_message`), a `requests` row (type `doctor_message`, the same text) and the message's
  id.
- Only a JSON `true` confirms; `"true"`, `"yes"` or `1` is refused.
- A call asks for the same thing once. The same refill again in one call (the same medication,
  however it is said) or the same message again returns what the first did and writes nothing:
  the database's unique constraints decide (`requests` on call, type and summary; `messages` on
  call, kind and body). A later call is a new request.
- A failure part-way (a database error, or the audit event not written) leaves nothing
  written, gives the model the generic failure and counts toward a hand-off.
- Limits:
  - Message text is the caller's, as the model wrote it down, and is stored as given except
    that SSNs are masked as the logs mask them: a grouped or nine-digit number, and the digits
    after a cue such as `ssn`, `last four` or `ending in`. Phone numbers, dates, names and
    anything else in it are stored unmasked. The cue rule also masks an innocent number after
    such words (`ends in 2 days`). An SSN said as number words without a cue (`one two three
    four five six seven eight nine`) is not masked.
  - A message id is a row number, so it tells roughly how many messages the clinic has.

### Admin API

A read-only JSON API for staff and graders, in `app/api/admin.py`, so the requests, messages
and audit log can be read and the chain checked without opening the database. It is off
unless `ADMIN_PASSWORD` is set: then it is mounted under `/admin/api`; without it those paths
answer `404` and the app logs `admin_api_off` once at start.

| Route (all `GET`) | Returns |
|---|---|
| `/admin/api/requests` | `{"requests": [...]}`: id, created_at, call_id, type, summary, patient_id |
| `/admin/api/messages` | `{"messages": [...]}`: id, created_at, call_id, kind, name, callback_number, body, patient_id |
| `/admin/api/audit` | `{"events": [...]}`: seq, ts, call_id, actor, action, resource_type, resource_id, outcome, reason; filters `call_id`, `action` |
| `/admin/api/audit/verify` | `{"ok", "checked", "breaks": [{"seq", "kind"}], "truncated"}`, as `AuditLog.verify` reports it (at most 100 breaks) |

```sh
curl -u "$ADMIN_USER:$ADMIN_PASSWORD" 'http://127.0.0.1:8000/admin/api/requests?limit=20'
```

- Sign-in: HTTP Basic with `ADMIN_USER` and `ADMIN_PASSWORD`. The user name and the password
  are each compared as a SHA-256 digest in constant time, both always. Anything else (no
  header, a wrong part, a malformed or non-UTF-8 header, an `Authorization` header over 1024
  characters) gets the same `401` and `WWW-Authenticate` challenge, and is logged as
  `admin_sign_in_failed failures=<n>`, never the name or password tried.
- Failed sign-ins are limited per client address (`app/guard/rate_limit.py`): after five
  inside a sliding minute the address gets `429`, before its credentials are looked at, until
  the oldest of them is a minute old. A correct sign-in is not counted. Failed sign-ins are
  not audited, so a stranger cannot make the audit chain grow.
- Paging: newest first; `limit` 1 to 100 (default 50); `before=<id or seq>` for the next page,
  the last id or seq of the page before. An unknown or repeated parameter, a value out of range
  or a filter that is not a call id or an audit action gets `422` with a fixed body. The verify
  route takes no parameters.
- One verification runs at a time: another asked for meanwhile gets `429` with `Retry-After: 5`,
  does no work and writes no event. It reads the whole chain, 1000 events a round trip, on the
  one worker that also carries the calls, so on a large chain it costs that worker and the
  database real time; run it when it is needed, not on a timer.
- Every successful read appends one audit event (actor `admin:<ADMIN_USER>`, action
  `admin_read_requests`, `admin_read_messages`, `admin_read_audit` or `admin_verify_audit`,
  resource type `request`, `message` or `audit_event`, no resource id, outcome `success`) in
  the transaction that read the rows. If the event cannot be written the answer is `503` and
  no rows. A database that cannot be reached is `503` too, with no detail.
- Every text value from the database passes the log masking (`mask`): SSNs, dates and email
  addresses in a summary or body are hidden. Patient ids and call ids come out as stored.
  Timestamps are ISO 8601.
- A callback number shows as `***-***-` and its last four digits, whatever its shape
  (`+44 20 7946 0958` shows as `***-***-0958`); with fewer than four digits it shows as
  `***-***-****`.
- Every admin response, errors included, carries `Cache-Control: no-store`. The routes are not
  in the API schema, and `/docs` stays off outside dev.
- The runtime role needs no new privilege: it already reads these tables and appends events.
- Limits:
  - Behind the HTTPS proxy every caller reaches the app from the proxy's address, so the
    sign-in limit is shared by everyone. Refused requests are not counted, so each failure
    ages out after a minute and is replaced by the next: one wrong password every 12 seconds,
    from anyone, keeps the admin locked out for as long as it goes on. A sustained lockout
    costs an attacker five requests a minute. It delays the admin; it is not a way in, and the
    password's strength is the real control. Trusting the proxy's forwarded address is left to
    the deploy set-up. The limit is held in memory, so a restart clears it, and it keeps at
    most 10,000 addresses, dropping the one that failed longest ago.
  - A real console would reveal a callback number only on an explicit, audited action; this
    API never reveals one, it shows the masked form with the last four digits.
  - Masking hides identifiers by their shape, as in the logs: names are not masked, and a
    number written another way (spelled out without a cue, for example) is shown as written.
  - A read event records which list was read, not which rows.

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
| `IDENTITY_HASH_KEY` | identity claim hash key, never logged | none, required |
| `MAX_CONCURRENT_CALLS` | 1 to 20, digits only             | `3`            |
| `MAX_CALL_SECONDS`  | 1 to 3600                           | `600`          |
| `TURN_HOST`         | host name, no scheme or port        | required unless dev |
| `TURN_SHARED_SECRET`| 32+ of `A-Z a-z 0-9 . _ ~ + / = -`, never logged | required unless dev |
| `DEMO_ACCESS_TOKEN` | at least 32 characters, never logged | required unless dev |
| `ADMIN_USER`        | 1 to 32 letters and `_`, starting with a letter | empty; required with `ADMIN_PASSWORD` |
| `ADMIN_PASSWORD`    | admin API password, never logged    | empty (admin API off) |
| `ANTHROPIC_API_KEY` | the model's API key, never logged    | required unless dev |
| `DEEPGRAM_API_KEY`  | speech-to-text's API key, never logged | required unless dev |
| `LLM_MODEL`         | a Claude model id (`claude-` and up to 60 of `a-z 0-9 . -`) | `claude-haiku-5-5` |

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
`TURN_SHARED_SECRET`, `AUDIT_HMAC_KEY`, `IDENTITY_HASH_KEY`, `ANTHROPIC_API_KEY`,
`DEEPGRAM_API_KEY`, `ADMIN_PASSWORD`) must also not start or end with whitespace and must
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
at least 16 characters. In every environment it must differ from every other secret of the
application's settings: every secret field of `Settings`, found from the class, so a secret
added later is covered too. A clash is reported by field name only. The owner's
`POSTGRES_PASSWORD` never reaches the app, so it cannot be compared there: choose another.

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
| `app`     | `APP_ENV`, `LOG_LEVEL`, `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `APP_DB_USER`, `APP_DB_PASSWORD`, `AUDIT_HMAC_KEY`, `IDENTITY_HASH_KEY`, `SIMULATED_NOW`, `MAX_CONCURRENT_CALLS`, `MAX_CALL_SECONDS`, `TURN_HOST`, `TURN_SHARED_SECRET`, `DEMO_ACCESS_TOKEN`, `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY`, `LLM_MODEL`, `ADMIN_USER`, `ADMIN_PASSWORD` |
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
