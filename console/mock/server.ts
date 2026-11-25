// A stand-in for the admin API, for `npm run dev:mock` only: it answers the console's routes with
// the fixtures (mock/fixtures.ts) under the same rules the real API keeps for a browser: CORS for
// the console's origin only, a session cookie, the anti-forgery token on every change, 401 without
// a session, and the live calls stream. It is never part of the console's build.
//
// MOCK_SCENARIO picks what it answers: "normal" (default), "empty" (nothing recorded yet),
// "error" (every read fails), "invalid" (answers of the wrong shape), "paused" (the stream pauses).
// MOCK_SIGNED_IN=1 treats every request as signed in, for screenshots.

import { createServer, type IncomingMessage, type ServerResponse } from "node:http";

import * as fixtures from "./fixtures.ts";

const PORT = Number(process.env.MOCK_API_PORT ?? 8001);
const CONSOLE_ORIGIN = process.env.MOCK_CONSOLE_ORIGIN ?? "http://localhost:3000";
const SCENARIO = process.env.MOCK_SCENARIO ?? "normal";
const ALWAYS_SIGNED_IN = process.env.MOCK_SIGNED_IN === "1";
const USER = "admin";
const PASSWORD = "mock-password";
const COOKIE = "admin_session";
const SESSION = "mock-session-id";
const PREFIX = "/admin/api";

let failedSignIns = 0;

function cors(request: IncomingMessage, response: ServerResponse) {
  if (request.headers.origin === CONSOLE_ORIGIN) {
    response.setHeader("Access-Control-Allow-Origin", CONSOLE_ORIGIN);
    response.setHeader("Access-Control-Allow-Credentials", "true");
    response.setHeader("Vary", "Origin");
  }
}

function send(
  response: ServerResponse,
  status: number,
  body?: unknown,
  headers: Record<string, string> = {},
) {
  response.writeHead(status, {
    "Cache-Control": "no-store",
    ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    ...headers,
  });
  response.end(body === undefined ? undefined : JSON.stringify(body));
}

function signedIn(request: IncomingMessage): boolean {
  return ALWAYS_SIGNED_IN || (request.headers.cookie ?? "").split(/;\s*/).includes(`${COOKIE}=${SESSION}`);
}

async function readBody(request: IncomingMessage): Promise<string> {
  let body = "";
  for await (const chunk of request) {
    body += chunk;
    if (body.length > 1024) break;
  }
  return body;
}

function page<T>(rows: T[], url: URL, key: (row: T) => number): T[] {
  const limit = Number(url.searchParams.get("limit") ?? 50);
  const before = url.searchParams.get("before");
  return rows.filter((row) => before === null || key(row) < Number(before)).slice(0, limit);
}

function read(path: string, url: URL): unknown {
  const now = new Date();
  const empty = SCENARIO === "empty";
  if (path === "/session") return fixtures.session();
  if (path === "/calls") {
    const reason = url.searchParams.get("end_reason");
    const calls = empty ? [] : fixtures.CALLS.filter((call) => reason === null || call.end_reason === reason);
    return { calls: page(calls, url, (call) => call.id) };
  }
  const detail = /^\/calls\/([0-9a-f]{32})\/detail$/.exec(path);
  if (detail) {
    const call = empty ? null : fixtures.callDetail(detail[1]);
    return call;
  }
  const turns = /^\/calls\/([0-9a-f]{32})\/turns$/.exec(path);
  if (turns) {
    const after = Number(url.searchParams.get("after") ?? 0);
    return { turns: fixtures.turnsOf(turns[1]).filter((turn) => turn.turn > after) };
  }
  if (path === "/queue") {
    const type = url.searchParams.get("type");
    const rows = empty ? [] : fixtures.QUEUE.filter((row) => type === null || row.type === type);
    return { requests: page(rows, url, (row) => row.id) };
  }
  if (path === "/appointments") {
    return { appointments: empty ? [] : page(fixtures.APPOINTMENTS, url, (row) => row.id) };
  }
  if (path === "/slots") {
    const from = url.searchParams.get("from") ?? now.toISOString().slice(0, 10);
    const days = Number(url.searchParams.get("days") ?? 7);
    return { slots: empty ? [] : fixtures.slots(from, days), truncated: false };
  }
  if (path === "/audit") {
    const callId = url.searchParams.get("call_id");
    const action = url.searchParams.get("action");
    const events = empty
      ? []
      : fixtures.AUDIT.filter(
          (event) =>
            (callId === null || event.call_id === callId) && (action === null || event.action === action),
        );
    return { events: page(events, url, (event) => event.seq) };
  }
  if (path === "/audit/verify") return fixtures.VERIFICATION;
  if (path === "/guardrails/summary") {
    const summary = fixtures.guardrails(Number(url.searchParams.get("days") ?? 7));
    return empty ? { ...summary, counts: [] } : summary;
  }
  if (path === "/metrics/summary") {
    const summary = fixtures.metrics(Number(url.searchParams.get("days") ?? 7));
    return empty
      ? {
          ...summary,
          calls_per_day: [],
          ended: [],
          verified_calls: 0,
          verification_attempts: 0,
          verification_successes: 0,
          latency: summary.latency.map((row) => ({ ...row, turns: 0, values: 0, p50: null, p95: null })),
          record_us_turns: 0,
          record_us_p50: null,
        }
      : summary;
  }
  return null;
}

function stream(request: IncomingMessage, response: ServerResponse) {
  response.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-store",
    "X-Accel-Buffering": "no",
  });
  // Nothing sent yet: the first list always goes, even an empty one.
  let last: string | null = null;
  const tick = () => {
    const calls = SCENARIO === "empty" ? [] : fixtures.liveCalls(new Date());
    // The fixtures' calls only change by their start times, which move with the clock: a change is
    // a call that came or went.
    const listed = calls.map((call) => call.call_id).join();
    if (listed !== last) {
      response.write(`event: calls\ndata: ${JSON.stringify(calls)}\n\n`);
      last = listed;
      if (SCENARIO === "paused") {
        response.write("event: paused\ndata: {}\n\n");
      }
    }
  };
  tick();
  const ticker = setInterval(tick, 2000);
  const comment = setInterval(() => response.write(": keep-alive\n\n"), 15_000);
  const renew = setTimeout(() => {
    response.write("event: renew\ndata: {}\n\n");
    response.end();
  }, 300_000);
  request.on("close", () => {
    clearInterval(ticker);
    clearInterval(comment);
    clearTimeout(renew);
  });
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url ?? "/", `http://localhost:${PORT}`);
  cors(request, response);
  if (!url.pathname.startsWith(PREFIX)) {
    send(response, 404, { detail: "Not Found" });
    return;
  }
  const path = url.pathname.slice(PREFIX.length);

  if (request.method === "OPTIONS") {
    if (request.headers.origin === CONSOLE_ORIGIN) {
      response.setHeader("Access-Control-Allow-Methods", "GET, POST, DELETE");
      response.setHeader("Access-Control-Allow-Headers", "Content-Type, X-CSRF-Token");
      response.setHeader("Access-Control-Max-Age", "600");
    }
    send(response, 204);
    return;
  }

  if (path === "/session" && request.method === "POST") {
    if (request.headers.origin !== CONSOLE_ORIGIN || request.headers["content-type"] !== "application/json") {
      send(response, 403, { detail: "Forbidden" });
      return;
    }
    if (failedSignIns >= 5) {
      send(response, 429, { detail: "Too many failed sign-ins" }, { "Retry-After": "60" });
      return;
    }
    let body: { user?: unknown; password?: unknown } = {};
    try {
      body = JSON.parse(await readBody(request));
    } catch {
      body = {};
    }
    if (body.user !== USER || body.password !== PASSWORD) {
      failedSignIns += 1;
      send(response, 401, { detail: "Sign-in failed" });
      return;
    }
    failedSignIns = 0;
    send(response, 200, fixtures.session(), {
      "Set-Cookie": `${COOKIE}=${SESSION}; HttpOnly; SameSite=Strict; Path=${PREFIX}`,
    });
    return;
  }

  if (!signedIn(request)) {
    send(response, 401, { detail: "Sign-in required" });
    return;
  }

  if (path === "/session" && request.method === "DELETE") {
    if (request.headers["x-csrf-token"] !== "csrf-fixture" || request.headers.origin !== CONSOLE_ORIGIN) {
      send(response, 403, { detail: "Forbidden" });
      return;
    }
    send(response, 204, undefined, {
      "Set-Cookie": `${COOKIE}=; HttpOnly; SameSite=Strict; Path=${PREFIX}; Max-Age=0`,
    });
    return;
  }

  if (request.method !== "GET") {
    send(response, 405, { detail: "Method Not Allowed" });
    return;
  }

  if (path === "/calls/live/stream") {
    stream(request, response);
    return;
  }
  if (SCENARIO === "error" && path !== "/session") {
    send(response, 503, { detail: "Service unavailable" });
    return;
  }
  if (SCENARIO === "invalid" && path !== "/session") {
    send(response, 200, { unexpected: true });
    return;
  }
  const body = read(path, url);
  if (body === null) {
    send(response, 404, { detail: "Not Found" });
    return;
  }
  send(response, 200, body);
});

server.listen(PORT, "127.0.0.1", () => {
  process.stdout.write(`mock admin API on http://localhost:${PORT} (scenario ${SCENARIO})\n`);
});
