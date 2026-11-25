import { focusManager } from "@tanstack/react-query";
import { act, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "@/components/app-shell";
import * as fixtures from "@/mock/fixtures";
import { fakeApi, json, renderConsole } from "@/tests/render-console";

vi.mock("next/navigation", () => ({
  usePathname: () => "/requests",
  useRouter: () => ({ replace: vi.fn() }),
}));

function api(calls: "on" | "off" | null) {
  return fakeApi((sent) =>
    sent.method === "GET" && sent.path === "/session"
      ? json({ ...fixtures.session(), calls })
      : json({ detail: "Not Found" }, 404),
  );
}

describe("the shell", () => {
  afterEach(() => {
    vi.useRealTimers();
    focusManager.setFocused(undefined);
  });

  it("shows the calls-off banner on every page when the session says the assistant is off", async () => {
    api("off");
    renderConsole(
      <AppShell>
        <p>page</p>
      </AppShell>,
    );

    expect(
      await screen.findByText("The phone assistant is switched off. New calls are refused."),
    ).toBeTruthy();
  });

  it("reads the session again every 60 seconds while the tab is shown, and not while it is hidden", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { sent } = api("on");
    const reads = () => sent.filter((each) => each.path === "/session").length;
    renderConsole(
      <AppShell>
        <p>page</p>
      </AppShell>,
    );
    await waitFor(() => expect(reads()).toBe(1));

    await act(async () => {
      vi.advanceTimersByTime(60_000);
    });
    await waitFor(() => expect(reads()).toBe(2));

    focusManager.setFocused(false);
    await act(async () => {
      vi.advanceTimersByTime(180_000);
    });
    expect(reads()).toBe(2);
  });
});
