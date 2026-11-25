import Link from "next/link";

import { shortId } from "@/lib/format";

// A call by its short id, linking to its detail page; the full id is in the link's name.
export function CallLink({ callId }: { callId: string }) {
  return (
    <Link
      href={`/calls/${callId}`}
      aria-label={`Call ${callId}`}
      title={callId}
      className="rounded-sm font-mono text-xs text-primary underline underline-offset-3 hover:text-primary/80"
    >
      {shortId(callId)}…
    </Link>
  );
}

export function CallId({ callId }: { callId: string }) {
  return (
    <span title={callId} className="font-mono text-xs">
      {shortId(callId)}…
    </span>
  );
}
