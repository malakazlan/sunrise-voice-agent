import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { openLiveStream, type LiveStreamHandlers } from "@/lib/api/stream";

const URL_ = "https://admin.clinic.test/admin/api/calls/live/stream";
const CALL = {
  call_id: "9fbe75cb1d2a4c3e8f00a1b2c3d4e5f6",
  started_at: "2026-11-25T15:42:00+00:00",
  turn: 3,
  phase: "serving",
  verified: true,
  patient_id: "P003",
  last_tool: "request_refill",
  vad_ms: 500,
  stt_ms: 80,
  first_token_ms: 1510,
  last_token_ms: 1900,
  first_audio_ms: 2060,
  total_ms: 2400,
};

class FakeEventSource {
  static made: FakeEventSource[] = [];
  readonly listeners = new Map<string, ((event: MessageEvent) => void)[]>();
  closed = false;

  constructor(
    readonly url: string,
    readonly init?: EventSourceInit,
  ) {
    FakeEventSource.made.push(this);
  }

  addEventListener(type: string, listener: (event: MessageEvent) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close() {
    this.closed = true;
  }

  emit(type: string, data = "") {
    for (const listener of this.listeners.get(type) ?? []) {
      listener(new MessageEvent(type, { data }));
    }
  }
}

function handlers() {
  return {
    onCalls: vi.fn<LiveStreamHandlers["onCalls"]>(),
    onState: vi.fn<LiveStreamHandlers["onState"]>(),
    onSignedOut: vi.fn<LiveStreamHandlers["onSignedOut"]>(),
    onConnectionLost: vi.fn<LiveStreamHandlers["onConnectionLost"]>(),
  };
}

const opened = () => FakeEventSource.made;
const open = () => FakeEventSource.made.filter((source) => !source.closed);

describe("the live calls stream", () => {
  let close: (() => void) | undefined;

  beforeEach(() => {
    vi.useFakeTimers();
    FakeEventSource.made = [];
    vi.stubGlobal("EventSource", FakeEventSource);
  });

  afterEach(() => {
    close?.();
    close = undefined;
    vi.useRealTimers();
  });

  it("opens one stream per tab, with the session cookie", () => {
    const first = handlers();
    openLiveStream(URL_, first);
    close = openLiveStream(URL_, handlers());

    expect(opened()).toHaveLength(2);
    expect(opened()[0].closed).toBe(true);
    expect(open()).toHaveLength(1);
    expect(open()[0].url).toBe(URL_);
    expect(open()[0].init).toEqual({ withCredentials: true });
  });

  it("shows the calls the API sent, and never an answer of the wrong shape", () => {
    const seen = handlers();
    close = openLiveStream(URL_, seen);

    open()[0].emit("calls", JSON.stringify([CALL]));
    expect(seen.onCalls).toHaveBeenCalledWith([CALL]);
    expect(seen.onState).toHaveBeenLastCalledWith("live");

    open()[0].emit("calls", JSON.stringify([{ ...CALL, phase: "dancing" }]));
    expect(seen.onCalls).toHaveBeenCalledTimes(1);
    expect(seen.onState).toHaveBeenLastCalledWith("invalid");
  });

  it("opens a new stream when the API asks for a renewal", () => {
    close = openLiveStream(URL_, handlers());
    const first = open()[0];

    first.emit("renew");

    expect(first.closed).toBe(true);
    expect(open()).toHaveLength(1);
    expect(open()[0]).not.toBe(first);
    expect(open()[0].url).toBe(URL_);
  });

  it("shows the paused state and keeps the last list when the API pauses", () => {
    const seen = handlers();
    close = openLiveStream(URL_, seen);
    open()[0].emit("calls", JSON.stringify([CALL]));

    open()[0].emit("paused");

    expect(seen.onState).toHaveBeenLastCalledWith("paused");
    expect(seen.onCalls).toHaveBeenCalledTimes(1);
    expect(open()).toHaveLength(1);
  });

  it("closes and sends the user to sign-in when the session ends", () => {
    const seen = handlers();
    close = openLiveStream(URL_, seen);

    open()[0].emit("signed_out");
    vi.advanceTimersByTime(60_000);

    expect(seen.onSignedOut).toHaveBeenCalledTimes(1);
    expect(open()).toHaveLength(0);
    expect(opened()).toHaveLength(1);
  });

  it("closes on unmount", () => {
    const stop = openLiveStream(URL_, handlers());
    expect(open()).toHaveLength(1);

    stop();

    expect(open()).toHaveLength(0);
  });

  it("does not come back after an unmount while it waited to retry", () => {
    const stop = openLiveStream(URL_, handlers());
    open()[0].emit("error");

    stop();
    vi.advanceTimersByTime(60_000);

    expect(open()).toHaveLength(0);
    expect(opened()).toHaveLength(1);
  });

  it("after a lost connection, retries on its own schedule and says it is retrying", () => {
    const seen = handlers();
    close = openLiveStream(URL_, seen);

    open()[0].emit("error");

    expect(open()).toHaveLength(0);
    expect(seen.onState).toHaveBeenLastCalledWith("retrying");
    expect(seen.onConnectionLost).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(2_000);
    expect(open()).toHaveLength(1);
  });
});
