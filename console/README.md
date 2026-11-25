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
(answers of the wrong shape) or `paused` (the stream pauses). `MOCK_SIGNED_IN=1` skips the
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
- **Bounded reads.** Nothing polls. Live calls come from the API's stream, one per tab, closed
  while the tab is hidden and on leaving the page, and retried on a growing delay after a lost
  connection. Every request has a timeout.
- **Security headers on every response** (`proxy.ts`, Next.js's name for middleware): a
  Content-Security-Policy with a fresh nonce per request (`default-src 'self'`; scripts and
  styles by nonce; `connect-src` the console and the admin API only; `frame-ancestors 'none'`;
  `base-uri 'none'`; `form-action 'self'`), `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer` and a `Permissions-Policy` denying camera, microphone and
  geolocation. No `X-Powered-By`. Fonts (IBM Plex, OFL) are bundled from `app/fonts`.
- **Times** are the clinic's, in the time zone the API names, said once in the top bar.
- **Light theme only.** Status is never shown by colour alone; every control is at least 44 px
  tall on touch screens; motion is off when the system asks for reduced motion.

## Tests

| Test                                   | What it holds                                                                                  |
| -------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `lib/api/client.test.ts`               | credentials, timeout, JSON only, the anti-forgery header, 401 to sign-in, the limiter's answer |
| `tests/no-stray-fetch.test.ts`         | no file but the fetch wrapper calls `fetch`; none but the stream client uses `EventSource`     |
| `tests/no-browser-storage.test.ts`     | no browser storage, cookie or console use in app code                                          |
| `lib/api/schemas.test.ts`              | an answer of the wrong shape is refused and ends in the error state                            |
| `tests/fixtures-not-shipped.test.ts`   | nothing of the fixture layer is in the build                                                   |
| `tests/no-remote-origins.test.ts`      | the browser's files load nothing from another origin                                           |
| `app/(shell)/metrics/metrics.test.tsx` | a figure the API left out reads "Not enough calls yet", never a number                         |
| `lib/api/stream.test.ts`               | one stream per tab, renew, paused, signed out, closed on unmount                               |

The CI `docker` job builds the image and checks `/health`, the headers and the nonce.
