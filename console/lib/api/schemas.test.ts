import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError } from "@/lib/api/client";
import { ApiProvider, useCalls } from "@/lib/api/hooks";
import {
  appointmentsSchema,
  callDetailSchema,
  callsSchema,
  guardrailsSchema,
  liveCallListSchema,
  metricsSchema,
  queueSchema,
  sessionSchema,
  slotsSchema,
  turnsSchema,
  verificationSchema,
} from "@/lib/api/schemas";
import * as fixtures from "@/mock/fixtures";

const NOW = new Date("2026-11-25T15:46:00Z");
const DETAIL = fixtures.callDetail(fixtures.fixtureCallId("43cfad67"));

function answering(body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>(async () => Response.json(body)),
  );
  return new ApiClient({ origin: "https://admin.clinic.test", onUnauthorized: vi.fn() });
}

describe("the answers' schemas", () => {
  it("accept what the API answers", () => {
    expect(sessionSchema.safeParse(fixtures.session()).success).toBe(true);
    expect(liveCallListSchema.safeParse(fixtures.liveCalls(NOW)).success).toBe(true);
    expect(callsSchema.safeParse({ calls: fixtures.CALLS }).success).toBe(true);
    expect(callDetailSchema.safeParse(DETAIL).success).toBe(true);
    expect(turnsSchema.safeParse({ turns: fixtures.turnsOf(DETAIL!.call.call_id) }).success).toBe(true);
    expect(queueSchema.safeParse({ requests: fixtures.QUEUE }).success).toBe(true);
    expect(appointmentsSchema.safeParse({ appointments: fixtures.APPOINTMENTS }).success).toBe(true);
    expect(slotsSchema.safeParse({ slots: fixtures.slots("2026-11-30", 7), truncated: false }).success).toBe(
      true,
    );
    expect(guardrailsSchema.safeParse(fixtures.guardrails(7)).success).toBe(true);
    expect(metricsSchema.safeParse(fixtures.metrics(7)).success).toBe(true);
    expect(verificationSchema.safeParse(fixtures.VERIFICATION).success).toBe(true);
  });

  it("refuse an answer of the wrong shape", () => {
    const call = fixtures.CALLS[0];
    const refused: [string, { safeParse(value: unknown): { success: boolean } }, unknown][] = [
      ["a missing list", callsSchema, {}],
      ["an unknown end reason", callsSchema, { calls: [{ ...call, end_reason: "vanished" }] }],
      ["a call id of another shape", callsSchema, { calls: [{ ...call, call_id: "../audit" }] }],
      ["a count as text", callsSchema, { calls: [{ ...call, turn_count: "6" }] }],
      ["an instant that is not one", callsSchema, { calls: [{ ...call, started_at: "today" }] }],
      [
        "a callback number that is not masked",
        queueSchema,
        { requests: [{ ...fixtures.QUEUE[5], callbacks: ["(555) 010-3344"] }] },
      ],
      [
        "a percentile that is not a whole number",
        metricsSchema,
        { ...fixtures.metrics(7), record_us_p50: 1.5 },
      ],
      ["a session without its token", sessionSchema, { ...fixtures.session(), csrf_token: "" }],
    ];
    for (const [what, schema, value] of refused) {
      expect(schema.safeParse(value).success, what).toBe(false);
    }
  });

  it("the fetch wrapper never hands on an answer of the wrong shape", async () => {
    const client = answering({ calls: [{ ...fixtures.CALLS[0], end_reason: "vanished" }] });

    const error = await client.get("/calls", callsSchema).then(
      () => null,
      (caught: unknown) => caught,
    );

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).kind).toBe("invalid_answer");
  });

  it("a page's query of an answer of the wrong shape ends in its error state, with no data", async () => {
    const client = answering({ calls: [{ ...fixtures.CALLS[0], turn_count: -1 }] });
    const queries = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(
        QueryClientProvider,
        { client: queries },
        createElement(ApiProvider, { client }, children),
      );

    const { result } = renderHook(() => useCalls(), { wrapper });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.data).toBeUndefined();
  });
});
