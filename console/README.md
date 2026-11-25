# Sunrise admin console

The admin console of the Sunrise voice agent: a Next.js app (App Router, TypeScript strict) that
shows staff and reviewers what the agent did. It runs in its own container at its own origin and
talks to one thing only, the admin API (`/admin/api/*`). Everything it shows is already masked
and audited by the API; the console holds no data, no secret and no business rule.

Stack: Next.js 16, React 19, Tailwind 4, shadcn/ui (components in `components/ui`), lucide icons,
TanStack Query, Recharts through shadcn's chart component, zod. Vitest for the tests. Node 22.

## Run it

```sh
npm ci
npm run dev:mock      # the console on :3000 against a mock admin API on :8001
```

`dev:mock` starts `mock/server.ts`, a stand-in for the admin API that serves the synthetic
fixtures in `mock/fixtures.ts` under the same browser rules (CORS for the console's origin
only, a session cookie, the anti-forgery token, the live calls stream). Sign in as `admin` with
`mock-password`. `MOCK_SCENARIO` picks what it answers: `normal`, `empty`, `error`, `invalid`
(answers of the wrong shape), `paused` (the stream pauses) or `unreadable` (the session cannot
say whether the assistant is on). The writes (marks, callback details, the two settings) are kept
in the mock's memory until it stops. `MOCK_SIGNED_IN=1` skips the
sign-in. The mock is a separate process the console reaches over HTTP; nothing of it is in the
build (`tests/fixtures-not-shipped.test.ts`).

Against a real admin API:

```sh
ADMIN_API_ORIGIN=http://localhost:8000 npm run dev
```

The API must list the console's origin as its `CONSOLE_ORIGIN`.

Production: `npm run build`, then `node .next/standalone/server.js` with `.next/static` copied
beside it, or the image (`Dockerfile`; Compose service `console`, see the root README).

## Checks

```sh
npm run lint          # ESLint, no warnings allowed
npm run format:check  # Prettier
npm run typecheck     # tsc --noEmit
npm run build         # next build
npm test              # Vitest; run after the build: two tests read its output
```

## Configuration

One setting, `ADMIN_API_ORIGIN`: the admin API's exact origin (`https://admin.example.org`; a
scheme, a host and an optional port, nothing else; `http` only for a loopback host). It is read
on the server when a request is served and handed to the browser by the root layout, so one
image works in every environment. With no valid value every answer is a 503. Nothing is set at
build time and there is no `NEXT_PUBLIC_*` value. Telemetry is off (`NEXT_TELEMETRY_DISABLED=1`).

## Rules

- **One way to the network.** `lib/api/client.ts` is the only place `fetch` is called: always to
  the admin API's origin, with the session cookie (`credentials: "include"`), a 10-second
  timeout and `Accept: application/json`; every state-changing request carries `X-CSRF-Token`
  and is refused before the network without one; a 401 sends the user to sign-in; every answer
  is checked by its zod schema (`lib/api/schemas.ts`) and one of the wrong shape is shown as an
  error, never rendered. `lib/api/stream.ts` is the only place a stream is opened.
- **Nothing kept in the browser.** The query cache lives in memory. No `localStorage`,
  `sessionStorage`, IndexedDB, cookie or console log in the app's code; URLs carry ids only.
  The session cookie belongs to the API's origin and is `HttpOnly`; the console never sees it.
- **Masked values stay masked.** Masks are shown as the API wrote them (set in a grey chip),
  never filled in or reformatted.
- **Bounded reads.** One read is on a timer: the shell reads `GET /session` again every 60 s
  while the tab is shown (the API does not audit it, and it does not extend the idle time), so
  the office state and the calls banner follow a change someone else made. The Config page reads
  the config when opened and after a change, never on window focus: each read is audited. Live
  calls come from the API's stream, one per tab, closed
  while the tab is hidden and on leaving the page, and retried after a lost connection on a
  growing delay (2 s, 4 s, 8 s … at most 30 s). When another tab of the same session opens the
  stream, this one stops and offers "Use here". Every request has a timeout: 10 s, and 120 s for
  the audit chain check.
- **The API's session rules.** A session is honoured only on a request that carries the
  console's `Origin`, which every request from the console does (`mode: "cors"`). Nothing in the
  console reads the session cookie or depends on its name. Sign-out always ends at the sign-in
  page, whatever the API answers.
- **Security headers on every response** (`proxy.ts`, Next.js's name for middleware): a
  Content-Security-Policy with a fresh nonce per request (`default-src 'self'`; scripts and
  styles by nonce; `connect-src` the console and the admin API only; `frame-ancestors 'none'`;
  `base-uri 'none'`; `form-action 'self'`), `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer` and a `Permissions-Policy` denying camera, microphone and
  geolocation. No `X-Powered-By`. Fonts (IBM Plex, OFL) are bundled from `app/fonts`.
- **The writes.** Mark handled and the two settings go through one confirmation dialog
  (`components/confirm-dialog.tsx`): its focus starts on Cancel, Cancel or Escape sends nothing,
  and a click outside does nothing. Show callback details has no dialog: it is its own explicit
  button, recorded in the audit log and limited by the API. A config change is sent as
  `{key, value, confirm: true}` only after the dialog is confirmed. No write is retried. Each
  row's write is awaited on its own, so two at once each get their answer. A row shows
  "Handled" only once the API has answered; a refused write leaves the row as it was and says
  why on it (a 403 asks to sign in again; a 429 says when to try again).
- **Callback details are held in one place.** The revealed name and number live in the
  Requests page's state only, for one row at a time: fetched by a plain call through the fetch
  wrapper (never a query or a mutation, so never in the query cache), never logged, never in a
  message, no copy button, and cleared on Hide, on leaving the page, when the list is read again
  and after 60 seconds. An answer that comes after another row was asked for, the list was read
  again or the page was left is dropped. A ten-digit number is shown grouped (`555 201 3344`),
  anything else as sent. Focus moves to the details when shown and back to the row's button when
  they hide; a live region says "Callback details shown" or "hidden", never the values.
  `next.config.ts` must not turn on `cacheComponents`: it keeps a left page alive, hidden.
- **The calls banner** reads `calls` from the session the shell re-reads every 60 s: switched
  off, every page says new calls are refused; unknown (`null`), a neutral notice that the
  settings could not be read. No page asks for the config to show it.
- **Times** are the clinic's, in the time zone the API names, said once in the top bar.
- **Light theme only.** Status is never shown by colour alone; every control is at least 44 px
  tall on touch screens; motion is off when the system asks for reduced motion.
- **Current browsers only** (the last two versions of Chrome, Edge, Firefox and Safari). The
  console uses `AbortSignal.any` and `AbortSignal.timeout`.

## Tests

| Test                                              | What it holds                                                                                                                                                                                                                                                         |
| ------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `lib/api/client.test.ts`                          | credentials, timeout, JSON only, the anti-forgery header, 401 to sign-in, the limiter's answer                                                                                                                                                                        |
| `app/providers.test.tsx`                          | a 401 anywhere sends the user to `/login`                                                                                                                                                                                                                             |
| `tests/no-stray-fetch.test.ts`                    | from each file's syntax tree: no file but the fetch wrapper names `fetch`; none but the stream client names `EventSource`                                                                                                                                             |
| `tests/no-browser-storage.test.ts`                | from each file's syntax tree: no browser storage, cookie, `console` or `dangerouslySetInnerHTML`, under any alias                                                                                                                                                     |
| `lib/api/schemas.test.ts`                         | an answer of the wrong shape is refused and ends in the error state                                                                                                                                                                                                   |
| `tests/fixtures-not-shipped.test.ts`              | nothing of the fixture layer is in the build                                                                                                                                                                                                                          |
| `tests/no-remote-origins.test.ts`                 | the browser's files load nothing from another origin                                                                                                                                                                                                                  |
| `app/(shell)/metrics/metrics.test.tsx`            | a figure the API left out reads "Not enough calls yet", never a number                                                                                                                                                                                                |
| `lib/api/stream.test.ts`                          | one stream per tab, renew, paused, replaced, signed out, the retry delays, closed when hidden and on unmount                                                                                                                                                          |
| `app/(shell)/requests/requests-callback.test.tsx` | callback details shown in the row only; none in the query or mutation cache, storage, address (decoded) or console; cleared on Hide, refetch, 60 s and unmount; a 429 or failure reveals nothing; one row at a time, a late answer dropped; focus and the live region |
| `lib/api/mutations.test.ts`                       | nothing is sent before the dialog is confirmed, Cancel sends nothing, and no write is retried even by a retrying client                                                                                                                                               |
| `app/(shell)/config/config.test.tsx`              | a config change is sent with `confirm: true` only after confirming; Cancel sends nothing; focus starts on Cancel; read on opening and after a change, not on focus                                                                                                    |
| `app/(shell)/requests/requests-handled.test.tsx`  | a row reads "Handled" only once the API answers; a failure or 403 leaves it unchanged and says so; two at once each get their answer; focus never on a disabled button                                                                                                |
| `components/settings-banner.test.tsx`             | calls off: new calls are refused; unknown: a neutral notice; on: nothing                                                                                                                                                                                              |
| `components/app-shell.test.tsx`                   | the shell shows the calls-off banner; it re-reads the session every 60 s while shown, not while hidden                                                                                                                                                                |
| `lib/format.test.ts`                              | a ten-digit callback number is grouped, anything else left as sent                                                                                                                                                                                                    |
| `tests/no-cache-components.test.ts`               | `next.config.ts` does not turn on `cacheComponents`                                                                                                                                                                                                                   |

The CI `docker` job builds the image and checks `/health`, every header, and the whole
Content-Security-Policy against the expected one with the nonce replaced by a placeholder.
