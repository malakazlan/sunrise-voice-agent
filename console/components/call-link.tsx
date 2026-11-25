import Link from "next/link";

import { isCallId } from "@/lib/api/schemas";
import { shortId } from "@/lib/format";

// A call by its short id, linking to its detail page; the full id is in the link's name. A value
// without a call id's shape is shown as it came, and is not a link.
export function CallLink({ callId }: { callId: string }) {
  if (!isCallId(callId)) {
    return <span className="font-mono text-xs break-all">{callId}</span>;
  }
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
