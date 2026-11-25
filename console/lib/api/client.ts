import type { ZodType } from "zod";

import { sessionSchema, type Session } from "@/lib/api/schemas";

// The one place the console calls `fetch`. Every request goes to the admin API's origin with the
// session cookie (`credentials: "include"`), has a timeout, asks for JSON and accepts only JSON
// that matches its schema. A state-changing request carries the session's anti-forgery token and
// is refused here, before the network, when there is none. A 401 on anything but the sign-in
// itself ends the session in the browser: `onUnauthorized` sends the user to the sign-in page.

export const API_PREFIX = "/admin/api";
const DEFAULT_TIMEOUT_MS = 10_000;

export type ApiErrorKind =
  | "unauthorized"
  | "rate_limited"
  | "not_found"
  | "rejected"
  | "unavailable"
  | "invalid_answer"
  | "timeout"
  | "network"
  | "no_session";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number | null;
  readonly retryAfterSeconds: number | null;

  constructor(kind: ApiErrorKind, status: number | null = null, retryAfterSeconds: number | null = null) {
    super(`admin API: ${kind}${status === null ? "" : ` (${status})`}`);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

export type QueryParams = Record<string, string | number | undefined>;

interface ApiClientOptions {
  origin: string;
  onUnauthorized: () => void;
  timeoutMs?: number;
}

interface Exchange {
  method: "GET" | "POST" | "DELETE";
  path: string;
  params?: QueryParams;
  body?: unknown;
  signal?: AbortSignal;
  // The sign-in itself: no token exists yet, and a 401 means the credentials were wrong.
  signingIn?: boolean;
}

function kindOf(status: number): ApiErrorKind {
  if (status === 401) return "unauthorized";
  if (status === 429) return "rate_limited";
  if (status === 404) return "not_found";
  if (status >= 500) return "unavailable";
  return "rejected";
}

function retryAfter(response: Response): number | null {
  const value = Number(response.headers.get("Retry-After"));
  return Number.isInteger(value) && value > 0 ? value : null;
}

export class ApiClient {
  readonly origin: string;
  private readonly onUnauthorized: () => void;
  private readonly timeoutMs: number;
  private csrfToken: string | null = null;

  constructor({ origin, onUnauthorized, timeoutMs = DEFAULT_TIMEOUT_MS }: ApiClientOptions) {
    this.origin = origin;
    this.onUnauthorized = onUnauthorized;
    this.timeoutMs = timeoutMs;
  }

  setCsrfToken(token: string | null): void {
    this.csrfToken = token;
  }

  url(path: string, params: QueryParams = {}): string {
    const url = new URL(`${API_PREFIX}${path}`, this.origin);
    for (const [name, value] of Object.entries(params)) {
      if (value !== undefined) {
        url.searchParams.set(name, String(value));
      }
    }
    return url.toString();
  }

  async get<T>(path: string, schema: ZodType<T>, params?: QueryParams, signal?: AbortSignal): Promise<T> {
    const response = await this.exchange({ method: "GET", path, params, signal });
    return this.parse(response, schema);
  }

  // The answer is the new session, its anti-forgery token included.
  async signIn(user: string, password: string): Promise<Session> {
    const response = await this.exchange({
      method: "POST",
      path: "/session",
      body: { user, password },
      signingIn: true,
    });
    const session = await this.parse(response, sessionSchema);
    this.csrfToken = session.csrf_token;
    return session;
  }

  async signOut(): Promise<void> {
    await this.exchange({ method: "DELETE", path: "/session" });
    this.csrfToken = null;
  }

  private async exchange({
    method,
    path,
    params,
    body,
    signal,
    signingIn = false,
  }: Exchange): Promise<Response> {
    const headers = new Headers({ Accept: "application/json" });
    if (body !== undefined) {
      headers.set("Content-Type", "application/json");
    }
    if (method !== "GET" && !signingIn) {
      if (this.csrfToken === null) {
        throw new ApiError("no_session");
      }
      headers.set("X-CSRF-Token", this.csrfToken);
    }
    const timeout = AbortSignal.timeout(this.timeoutMs);
    let response: Response;
    try {
      response = await fetch(this.url(path, params), {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "include",
        mode: "cors",
        cache: "no-store",
        redirect: "error",
        referrerPolicy: "no-referrer",
        signal: signal === undefined ? timeout : AbortSignal.any([signal, timeout]),
      });
    } catch (error) {
      if (timeout.aborted) {
        throw new ApiError("timeout");
      }
      if (signal?.aborted) {
        throw error;
      }
      throw new ApiError("network");
    }
    if (response.ok) {
      return response;
    }
    const kind = kindOf(response.status);
    if (kind === "unauthorized" && !signingIn) {
      this.csrfToken = null;
      this.onUnauthorized();
    }
    throw new ApiError(kind, response.status, retryAfter(response));
  }

  private async parse<T>(response: Response, schema: ZodType<T>): Promise<T> {
    if (!(response.headers.get("Content-Type") ?? "").startsWith("application/json")) {
      throw new ApiError("invalid_answer", response.status);
    }
    let body: unknown;
    try {
      body = await response.json();
    } catch {
      throw new ApiError("invalid_answer", response.status);
    }
    const parsed = schema.safeParse(body);
    if (!parsed.success) {
      throw new ApiError("invalid_answer", response.status);
    }
    return parsed.data;
  }
}
