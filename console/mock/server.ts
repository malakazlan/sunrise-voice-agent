// A stand-in for the admin API, for `npm run dev:mock` only: it answers the console's routes with
// the fixtures (mock/fixtures.ts) under the same rules the real API keeps for a browser: CORS for
// the console's origin only, a session cookie, the anti-forgery token on every change, 401 without
// a session, and the live calls stream; and the writes (marks, callback reveals limited to 30 a
// minute, the two settings), kept in memory while it runs. It is never part of the console's build.
//
// MOCK_SCENARIO picks what it answers: "normal" (default), "empty" (nothing recorded yet),
// "error" (every read fails), "invalid" (answers of the wrong shape), "paused" (the stream pauses
// once, two seconds in, and the next list clears it), "unreadable" (the session cannot say whether
// the assistant is on: the settings could not be read). MOCK_SIGNED_IN=1 treats every request from
// the console's origin as signed in, for screenshots. As the API does, a session is honoured only
// on a request that carries the console's Origin, and the cookie's name is the mock's own: the
// console never reads it.

import { createServer, type IncomingMessage, type ServerResponse } from "node:http";

import * as fixtures from "./fixtures.ts";

const PORT = Number(process.env.MOCK_API_PORT ?? 8001);
const CONSOLE_ORIGIN = process.env.MOCK_CONSOLE_ORIGIN ?? "http://localhost:3000";
const SCENARIO = process.env.MOCK_SCENARIO ?? "normal";
const ALWAYS_SIGNED_IN = process.env.MOCK_SIGNED_IN === "1";
const USER = "admin";
const PASSWORD = "mock-password";
const COOKIE = "mock_session";
const SESSION = "mock-session-id";
const PREFIX = "/admin/api";

let failedSignIns = 0;

// The writes' state, for as long as the mock runs: the requests marked handled, and the two
// settings with who changed them last.
const marks = new Map(
  fixtures.QUEUE.flatMap((row) => (row.handled === null ? [] : [[row.id, row.handled] as const])),
);
const settings = fixtures.config().config;
// When each callback reveal of the last minute was answered: 30 a minute, as the API allows.
const reveals: number[] = [];

function currentSession() {
  if (SCENARIO === "unreadable") {
    return { ...fixtures.session(), calls: null };
  }
  const office = settings.office.value;
  return {
    ...fixtures.session(),
    calls: settings.calls.value,
    office_open: office === "schedule" ? fixtures.session().office_open : office === "open",
  };
}

// A write under the session: the anti-forgery token and the console's origin, and a small JSON
// body; its answer, or null when it is refused (the answer already sent).
async function writeBody(
  request: IncomingMessage,
  response: ServerResponse,
): Promise<Record<string, unknown> | null> {
  if (request.headers["x-csrf-token"] !== "csrf-fixture" || request.headers.origin !== CONSOLE_ORIGIN) {
    send(response, 403, { detail: "Forbidden" });
    return null;
  }
  if (request.headers["content-type"] !== "application/json") {
    send(response, 415, { detail: "Unsupported Media Type" });
    return null;
  }
  const text = await readBody(request);
  if (text.length > 1024) {
    send(response, 413, { detail: "Request too large" });
    return null;
  }
  try {
    const body: unknown = JSON.parse(text);
    if (typeof body === "object" && body !== null && !Array.isArray(body)) {
      return body as Record<string, unknown>;
    }
  } catch {
    // Answered below as invalid.
  }
  send(response, 422, { detail: "Invalid request" });
  return null;
}

async function write(path: string, request: IncomingMessage, response: ServerResponse): Promise<boolean> {
  const handled = /^\/queue\/requests\/(\d+)\/handled$/.exec(path);
  const callback = /^\/queue\/requests\/(\d+)\/callback$/.exec(path);
  if (!handled && !callback && path !== "/config") return false;
  const body = await writeBody(request, response);
  if (body === null) return true;
  if ((handled || callback) && Object.keys(body).length > 0) {
    send(response, 422, { detail: "Invalid request" });
    return true;
  }
  if (handled) {
    const id = Number(handled[1]);
    if (!fixtures.QUEUE.some((row) => row.id === id)) {
      send(response, 404, { detail: "Not Found" });
      return true;
    }
    const existing = marks.get(id);
    const mark = existing ?? {
      by: fixtures.session().user,
      at: new Date().toISOString().replace("Z", "+00:00"),
    };
    marks.set(id, mark);
    send(response, existing ? 200 : 201, { source: "requests", id, handled: mark });
    return true;
  }
  if (callback) {
    const now = Date.now();
    while (reveals.length > 0 && reveals[0] <= now - 60_000) reveals.shift();
    if (reveals.length >= 30) {
      const wait = Math.ceil((reveals[0] + 60_000 - now) / 1000);
      send(response, 429, { detail: "Too many requests" }, { "Retry-After": String(wait) });
      return true;
    }
    reveals.push(now);
    const id = Number(callback[1]);
    const details = fixtures.CALLBACK_DETAILS[id];
    if (details === undefined) {
      send(response, 404, { detail: "Not Found" });
      return true;
    }
    send(response, 200, { source: "requests", id, callbacks: details });
    return true;
  }
  const { key, value, confirm, ...rest } = body;
  const values: Record<string, string[]> = { calls: ["on", "off"], office: ["schedule", "open", "closed"] };
  if (
    confirm !== true ||
    Object.keys(rest).length > 0 ||
    (key !== "calls" && key !== "office") ||
    typeof value !== "string" ||
    !values[key].includes(value)
  ) {
    send(response, 422, { detail: "Invalid request" });
    return true;
  }
  const current = settings[key];
  const changed = current.value !== value;
  if (changed) {
    Object.assign(current, {
      value,
      changed_by: fixtures.session().user,
      changed_at: new Date().toISOString().replace("Z", "+00:00"),
    });
  }
  send(response, changed ? 201 : 200, { key, ...current, applies_within_seconds: 5 });
  return true;
}

function cors(request: IncomingMessage, response: ServerResponse) {
  if (request.headers.origin === CONSOLE_ORIGIN) {
    response.setHeader("Access-Control-Allow-Origin", CONSOLE_ORIGIN);
    response.setHeader("Access-Control-Allow-Credentials", "true");
    response.setHeader("Access-Control-Expose-Headers", "Retry-After");
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
  if (request.headers.origin !== CONSOLE_ORIGIN) {
    return false;
  }
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
  if (path === "/session") return currentSession();
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
    const state = url.searchParams.get("state") ?? "todo";
    const rows = (empty ? [] : fixtures.QUEUE)
      .map((row) => ({ ...row, handled: marks.get(row.id) ?? null }))
      .filter((row) => type === null || row.type === type)
      .filter((row) => state === "all" || (state === "handled") === (row.handled !== null));
    return { requests: page(rows, url, (row) => row.id) };
  }
  if (path === "/config") {
    return { ...fixtures.config(), config: settings };
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

// The one session's open stream: a newer one replaces it, as the API does.
let openStream: ServerResponse | null = null;

function stream(request: IncomingMessage, response: ServerResponse) {
  response.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-store",
    "X-Accel-Buffering": "no",
  });
  if (openStream !== null) {
    openStream.write("event: replaced\ndata: {}\n\n");
    openStream.end();
  }
  openStream = response;
  // Nothing sent yet: the first list always goes, even an empty one.
  let last: string | null = null;
  let ticks = 0;
  const tick = () => {
    ticks += 1;
    if (SCENARIO === "paused" && ticks === 2) {
      response.write("event: paused\ndata: {}\n\n");
      // After a pause the next list always goes.
      last = null;
      return;
    }
    const calls = SCENARIO === "empty" ? [] : fixtures.liveCalls(new Date());
    // The fixtures' calls only change by their start times, which move with the clock: a change is
    // a call that came or went.
    const listed = calls.map((call) => call.call_id).join();
    if (listed !== last) {
      response.write(`event: calls\ndata: ${JSON.stringify(calls)}\n\n`);
      last = listed;
    }
  };
  tick();
  const ticker = setInterval(tick, 2000);
  const comment = setInterval(() => response.write(": keep-alive\n\n"), 15_000);
  const renew = setTimeout(() => {
    response.write("event: renew\ndata: {}\n\n");
    response.end();
  }, 300_000);
  response.on("close", () => {
    clearInterval(ticker);
    clearInterval(comment);
    clearTimeout(renew);
    if (openStream === response) {
      openStream = null;
    }
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
    send(response, 200, currentSession(), {
      "Set-Cookie": `${COOKIE}=${SESSION}; HttpOnly; SameSite=Strict; Path=${PREFIX}`,
    });
    return;
  }

  // Sign-out always answers 204 and ends whatever session there was.
  if (path === "/session" && request.method === "DELETE") {
    send(response, 204, undefined, {
      "Set-Cookie": `${COOKIE}=; HttpOnly; SameSite=Strict; Path=${PREFIX}; Max-Age=0`,
    });
    return;
  }

  if (!signedIn(request)) {
    send(response, 401, { detail: "Sign-in required" });
    return;
  }

  if (request.method === "POST" && (await write(path, request, response))) {
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
