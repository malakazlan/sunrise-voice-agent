// @vitest-environment node
import { describe, expect, it, vi } from "vitest";
import { z } from "zod";

import { ApiClient, ApiError } from "@/lib/api/client";

const ORIGIN = "https://admin.clinic.test";
const answer = z.object({ calls: z.array(z.object({ call_id: z.string() })) });

function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function setUp(respond: () => Promise<Response>) {
  const fetchMock = vi.fn<typeof fetch>(respond);
  vi.stubGlobal("fetch", fetchMock);
  const onUnauthorized = vi.fn();
  const client = new ApiClient({ origin: ORIGIN, onUnauthorized, timeoutMs: 50 });
  return { client, fetchMock, onUnauthorized };
}

function sent(fetchMock: ReturnType<typeof vi.fn<typeof fetch>>, index = 0) {
  const [url, init] = fetchMock.mock.calls[index];
  return { url: String(url), init: init ?? {}, headers: new Headers(init?.headers) };
}

async function failure(promise: Promise<unknown>): Promise<ApiError> {
  const error = await promise.then(
    () => null,
    (caught: unknown) => caught,
  );
  expect(error).toBeInstanceOf(ApiError);
  return error as ApiError;
}

describe("the fetch wrapper", () => {
  it("reads from the admin API with the session cookie, a timeout and no anti-forgery header", async () => {
    const { client, fetchMock } = setUp(async () => json({ calls: [{ call_id: "a1" }] }));

    const result = await client.get("/calls", answer, { limit: 50, before: undefined });

    expect(result).toEqual({ calls: [{ call_id: "a1" }] });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const { url, init, headers } = sent(fetchMock);
    expect(url).toBe("https://admin.clinic.test/admin/api/calls?limit=50");
    expect(init.method).toBe("GET");
    expect(init.credentials).toBe("include");
    expect(init.signal).toBeInstanceOf(AbortSignal);
    expect(headers.get("Accept")).toBe("application/json");
    expect(headers.has("X-CSRF-Token")).toBe(false);
  });

  it("sends the session's anti-forgery token on a state-changing request", async () => {
    const { client, fetchMock } = setUp(async () => new Response(null, { status: 204 }));
    client.setCsrfToken("token-from-session");

    await client.signOut();

    const { url, init, headers } = sent(fetchMock);
    expect(url).toBe("https://admin.clinic.test/admin/api/session");
    expect(init.method).toBe("DELETE");
    expect(init.credentials).toBe("include");
    expect(headers.get("X-CSRF-Token")).toBe("token-from-session");
  });

  it("refuses a state-changing request without a token before it reaches the network", async () => {
    const { client, fetchMock } = setUp(async () => new Response(null, { status: 204 }));

    const error = await failure(client.signOut());

    expect(error.kind).toBe("no_session");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("sends the user to sign-in when a read answers 401", async () => {
    const { client, onUnauthorized } = setUp(async () => json({ detail: "Sign-in required" }, 401));

    const error = await failure(client.get("/calls", answer));

    expect(error.kind).toBe("unauthorized");
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("signs in with JSON and the cookie, and a refused sign-in stays on the form", async () => {
    const { client, fetchMock, onUnauthorized } = setUp(async () => json({ detail: "Sign-in failed" }, 401));

    const error = await failure(client.signIn("admin", "a-password"));

    expect(error.kind).toBe("unauthorized");
    expect(onUnauthorized).not.toHaveBeenCalled();
    const { url, init, headers } = sent(fetchMock);
    expect(url).toBe("https://admin.clinic.test/admin/api/session");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(JSON.parse(String(init.body))).toEqual({ user: "admin", password: "a-password" });
  });

  it("takes the anti-forgery token from the sign-in's answer", async () => {
    const session = {
      user: "admin",
      csrf_token: "token-from-sign-in",
      expires_in: 1800,
      office_open: true,
      time_zone: "America/New_York",
    };
    const { client, fetchMock } = setUp(async () =>
      fetchMock.mock.calls.length === 1 ? json(session) : new Response(null, { status: 204 }),
    );

    await client.signIn("admin", "a-password");
    await client.signOut();

    expect(sent(fetchMock, 0).headers.has("X-CSRF-Token")).toBe(false);
    expect(sent(fetchMock, 1).headers.get("X-CSRF-Token")).toBe("token-from-sign-in");
  });

  it("tells the sign-in form when the limiter has stopped it", async () => {
    const { client } = setUp(async () =>
      json({ detail: "Too many failed sign-ins" }, 429, { "Retry-After": "60" }),
    );

    const error = await failure(client.signIn("admin", "a-password"));

    expect(error.kind).toBe("rate_limited");
    expect(error.retryAfterSeconds).toBe(60);
  });

  it("gives up on a request that takes too long", async () => {
    const { client } = setUp(
      () =>
        new Promise<Response>((_resolve, reject) => {
          const signal = vi.mocked(fetch).mock.calls[0][1]?.signal;
          signal?.addEventListener("abort", () => reject(signal.reason));
        }),
    );

    const error = await failure(client.get("/calls", answer));

    expect(error.kind).toBe("timeout");
  });

  it("gives up on time even when the caller's own signal never aborts", async () => {
    const { client } = setUp(
      () =>
        new Promise<Response>((_resolve, reject) => {
          const signal = vi.mocked(fetch).mock.calls[0][1]?.signal;
          signal?.addEventListener("abort", () => reject(signal.reason));
        }),
    );
    const neverAborts = new AbortController().signal;

    const error = await failure(client.get("/calls", answer, undefined, { signal: neverAborts }));

    expect(error.kind).toBe("timeout");
  }, 2_000);

  it("waits longer for a read given a longer timeout", async () => {
    const { client } = setUp(
      () =>
        new Promise<Response>((resolve, reject) => {
          const signal = vi.mocked(fetch).mock.calls[0][1]?.signal;
          signal?.addEventListener("abort", () => reject(signal.reason));
          setTimeout(() => resolve(json({ calls: [] })), 150);
        }),
    );

    await expect(client.get("/calls", answer, undefined, { timeoutMs: 1_000 })).resolves.toEqual({
      calls: [],
    });
  });

  it("refuses an answer that is not JSON", async () => {
    const { client } = setUp(
      async () => new Response("<html></html>", { headers: { "Content-Type": "text/html" } }),
    );

    const error = await failure(client.get("/calls", answer));

    expect(error.kind).toBe("invalid_answer");
  });
});
