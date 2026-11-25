import { liveCallListSchema, type LiveCall } from "@/lib/api/schemas";

// The one place the console opens a stream: the admin API's live calls stream, at most one per
// tab. The API sends `calls` (the list, when it changed, and always after a `paused`), `renew`
// (the stream's time is up: open a new one), `paused` (a read failed; the API retries),
// `replaced` (another tab of the same session opened a stream: this one stops for good) and
// `signed_out` (the session ended). A lost connection is closed here rather than left to the
// browser's own retry, and retried on a growing delay, so that a refused stream is not hammered.

export type LiveStreamState = "connecting" | "live" | "paused" | "retrying" | "invalid" | "replaced";

export interface LiveStreamHandlers {
  onCalls: (calls: LiveCall[]) => void;
  onState: (state: LiveStreamState) => void;
  onSignedOut: () => void;
  // The connection was lost: the caller checks whether the session still holds.
  onConnectionLost: () => void;
}

const FIRST_RETRY_MS = 2_000;
const LONGEST_RETRY_MS = 30_000;

let closeCurrent: (() => void) | null = null;

export function openLiveStream(url: string, handlers: LiveStreamHandlers): () => void {
  closeCurrent?.();

  let source: EventSource | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let retryDelay = FIRST_RETRY_MS;

  const stopSource = () => {
    source?.close();
    source = null;
  };

  const connect = () => {
    stopSource();
    handlers.onState("connecting");
    const opened = new EventSource(url, { withCredentials: true });
    source = opened;

    opened.addEventListener("calls", (event) => {
      let payload: unknown;
      try {
        payload = JSON.parse((event as MessageEvent<string>).data);
      } catch {
        payload = undefined;
      }
      const parsed = liveCallListSchema.safeParse(payload);
      if (!parsed.success) {
        handlers.onState("invalid");
        return;
      }
      retryDelay = FIRST_RETRY_MS;
      handlers.onCalls(parsed.data);
      handlers.onState("live");
    });
    opened.addEventListener("renew", () => {
      connect();
    });
    opened.addEventListener("paused", () => {
      handlers.onState("paused");
    });
    opened.addEventListener("replaced", () => {
      close();
      handlers.onState("replaced");
    });
    opened.addEventListener("signed_out", () => {
      close();
      handlers.onSignedOut();
    });
    opened.addEventListener("error", () => {
      if (source !== opened) {
        return;
      }
      stopSource();
      handlers.onState("retrying");
      handlers.onConnectionLost();
      retryTimer = setTimeout(() => {
        retryTimer = null;
        connect();
      }, retryDelay);
      retryDelay = Math.min(retryDelay * 2, LONGEST_RETRY_MS);
    });
  };

  const close = () => {
    if (retryTimer !== null) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    stopSource();
    if (closeCurrent === close) {
      closeCurrent = null;
    }
  };

  closeCurrent = close;
  connect();
  return close;
}
