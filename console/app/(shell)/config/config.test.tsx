import { focusManager } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { ConfigView } from "@/app/(shell)/config/config";
import * as fixtures from "@/mock/fixtures";
import { fakeApi, json, renderConsole, type Sent } from "@/tests/render-console";

const NOW = "2026-11-25T17:30:05.123456+00:00";

function api(
  change: (sent: Sent) => Response = (sent) => {
    const { key, value } = sent.body as { key: string; value: string };
    return json({ key, value, changed_by: "clinic_admin", changed_at: NOW, applies_within_seconds: 5 }, 201);
  },
) {
  return fakeApi((sent) => {
    if (sent.method === "GET" && sent.path === "/config") return json(fixtures.config());
    if (sent.method === "POST" && sent.path === "/config") return change(sent);
    return json({ detail: "Not Found" }, 404);
  });
}

async function choose(group: string, option: string) {
  const control = await screen.findByRole("radiogroup", { name: group });
  fireEvent.click(within(control).getByRole("radio", { name: option }));
  return screen.findByRole("alertdialog");
}

describe("Config", () => {
  it("asks before switching the assistant off, and Cancel sends nothing", async () => {
    const { writes } = api();
    renderConsole(<ConfigView />);

    const dialog = await choose("Phone assistant", "Off");

    expect(dialog.textContent).toContain(
      "New callers will be told the assistant is unavailable and to call the front desk. Calls in progress continue.",
    );
    expect(writes()).toEqual([]);
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(writes()).toEqual([]);
  });

  it("starts the dialog's focus on Cancel and draws switching off as destructive", async () => {
    api();
    renderConsole(<ConfigView />);

    const dialog = await choose("Phone assistant", "Off");
    const action = within(dialog).getByRole("button", { name: "Switch the assistant off" });

    await waitFor(() => expect(document.activeElement?.textContent).toBe("Cancel"));
    expect(document.activeElement).not.toBe(action);
    expect(action.getAttribute("data-variant")).toBe("destructive");
  });

  it("sends the change with confirm: true only once it is confirmed, then says when it applies", async () => {
    const { writes } = api();
    renderConsole(<ConfigView />);

    const dialog = await choose("Phone assistant", "Off");
    expect(writes()).toEqual([]);
    fireEvent.click(within(dialog).getByRole("button", { name: "Switch the assistant off" }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0].body).toEqual({ key: "calls", value: "off", confirm: true });
    expect(writes()[0].headers.get("X-CSRF-Token")).toBe("csrf-test");
    expect(await screen.findByText(/Applies within 5 seconds/)).toBeTruthy();
  });

  it("says what callers will be told before the office is treated as closed", async () => {
    const { writes } = api();
    renderConsole(<ConfigView />);

    const dialog = await choose("Office hours", "Closed");

    expect(dialog.textContent).toContain("Callers who ring after this are told the office is closed");
    fireEvent.click(within(dialog).getByRole("button", { name: "Treat as closed" }));
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0].body).toEqual({ key: "office", value: "closed", confirm: true });
  });

  it("shows an unchanged setting as such", async () => {
    api((sent) => {
      const { key, value } = sent.body as { key: string; value: string };
      return json({ key, value, changed_by: null, changed_at: null, applies_within_seconds: 5 }, 200);
    });
    renderConsole(<ConfigView />);

    const dialog = await choose("Office hours", "Open");
    fireEvent.click(within(dialog).getByRole("button", { name: "Treat as open" }));

    expect(await screen.findByText(/Unchanged: the office was already open/)).toBeTruthy();
  });

  it("says the session has changed when the anti-forgery check refuses the change", async () => {
    api(() => json({ detail: "Forbidden" }, 403));
    renderConsole(<ConfigView />);

    const dialog = await choose("Phone assistant", "Off");
    fireEvent.click(within(dialog).getByRole("button", { name: "Switch the assistant off" }));

    expect(await screen.findByText("Your session has changed. Sign in again.")).toBeTruthy();
  });
});

// Each read of the config is an audited event: it is read when the page is opened and after a
// change, not whenever the window gets focus.
describe("Config reads", () => {
  afterEach(() => {
    focusManager.setFocused(undefined);
  });

  const reads = (sent: Sent[]) =>
    sent.filter((each) => each.method === "GET" && each.path === "/config").length;

  it("are not repeated when the window gets focus", async () => {
    const { sent } = api();
    renderConsole(<ConfigView />, { defaultOptions: { queries: { staleTime: 0 } } });
    await screen.findByRole("radiogroup", { name: "Phone assistant" });

    await act(async () => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });

    expect(reads(sent)).toBe(1);
  });

  it("happen each time the page is opened, even when the last is recent", async () => {
    const { sent } = api();
    function OpenedTwice() {
      const [opened, setOpened] = useState(1);
      return (
        <>
          <button onClick={() => setOpened(opened + 1)}>Open again</button>
          <ConfigView key={opened} />
        </>
      );
    }
    renderConsole(<OpenedTwice />, { defaultOptions: { queries: { staleTime: 15_000 } } });
    await screen.findByRole("radiogroup", { name: "Phone assistant" });

    fireEvent.click(screen.getByRole("button", { name: "Open again" }));

    await waitFor(() => expect(reads(sent)).toBe(2));
  });

  it("happen again after a change", async () => {
    const { sent } = api();
    renderConsole(<ConfigView />, { defaultOptions: { queries: { staleTime: 15_000 } } });

    const dialog = await choose("Phone assistant", "Off");
    fireEvent.click(within(dialog).getByRole("button", { name: "Switch the assistant off" }));

    await waitFor(() => expect(reads(sent)).toBe(2));
  });
});
