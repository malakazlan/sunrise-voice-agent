"use client";

import { Link2, ShieldAlert, ShieldCheck, ShieldQuestion } from "lucide-react";
import { useId, useState, type FormEvent } from "react";

import { CallLink } from "@/components/call-link";
import { useClinic } from "@/components/clinic";
import { PageHeader, Section } from "@/components/page-header";
import { OutcomeBadge } from "@/components/status-badge";
import { EmptyState, ErrorState, ShowMore, TableSkeleton } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ApiError } from "@/lib/api/client";
import { allRows, useAudit, useVerifyChain, type AuditFilter } from "@/lib/api/hooks";
import { isCallId } from "@/lib/api/schemas";
import { clinicDateTime, plural } from "@/lib/format";
import { AUDIT_ACTIONS } from "@/lib/labels";
import { cn } from "@/lib/utils";

const ALL_ACTIONS = "all";

function ChainPanel() {
  const verify = useVerifyChain();
  let tone: "neutral" | "good" | "broken" = "neutral";
  let Icon = ShieldQuestion;
  let title = "Not checked in this session";
  let lines: string[] = [
    "Each event is sealed to the one before it, so an edit or a deletion anywhere shows as a break.",
  ];

  if (verify.isPending) {
    title = "Checking the chain…";
    lines = ["Every event is read and its seal checked. On a long log this takes a while."];
  } else if (verify.isError) {
    const busy = verify.error instanceof ApiError && verify.error.kind === "rate_limited";
    title = busy ? "A check is already running" : "The check could not be run";
    lines = busy
      ? [
          `Only one check runs at a time. Try again in ${verify.error instanceof ApiError && verify.error.retryAfterSeconds ? `${verify.error.retryAfterSeconds} seconds` : "a moment"}.`,
        ]
      : ["The admin API did not answer the check. Nothing is known about the chain from this attempt."];
  } else if (verify.data) {
    const result = verify.data;
    if (result.ok) {
      tone = "good";
      Icon = ShieldCheck;
      title = "Chain intact";
      lines = [`${plural(result.checked, "event")} checked; every seal matches.`];
    } else {
      tone = "broken";
      Icon = ShieldAlert;
      const first = result.breaks.find((found) => found.seq !== null);
      title = first ? `Chain broken at event ${first.seq}` : "Chain broken";
      lines = [
        `${plural(result.checked, "event")} checked; ${plural(result.breaks.length, "break")} listed:`,
        ...result.breaks.map(
          (found) =>
            `${found.seq === null ? "An event whose position is not known" : `Event ${found.seq}`}: ${found.kind.replaceAll("_", " ")}`,
        ),
      ];
    }
    if (result.truncated) {
      lines.push("More breaks were found than are listed here.");
    }
  }

  return (
    <section
      aria-live="polite"
      className={cn(
        "mb-6 flex flex-col gap-4 rounded-xl border border-l-4 px-5 py-4 sm:flex-row sm:items-center",
        tone === "good" && "border-success/30 border-l-success bg-success-soft/60",
        tone === "broken" && "border-destructive/30 border-l-destructive bg-destructive/5",
        tone === "neutral" && "border-primary/20 border-l-primary bg-secondary/60",
      )}
    >
      <span
        className={cn(
          "flex size-10 shrink-0 items-center justify-center rounded-full text-white",
          tone === "good" ? "bg-success" : tone === "broken" ? "bg-destructive" : "bg-primary",
        )}
      >
        <Icon className="size-5" aria-hidden />
      </span>
      <div className="min-w-0 flex-1">
        <h2 className="font-heading font-semibold">{title}</h2>
        {lines.map((line, index) => (
          <p key={index} className="text-sm text-muted-foreground">
            {line}
          </p>
        ))}
      </div>
      <Button onClick={() => verify.mutate()} disabled={verify.isPending} className="h-10 px-4">
        <Link2 aria-hidden />
        {verify.isPending ? "Checking…" : "Check the chain now"}
      </Button>
    </section>
  );
}

function record(resourceType: string, resourceId: string | null) {
  if (resourceId === null || resourceType === "call") {
    return resourceType.replaceAll("_", " ");
  }
  return (
    <>
      {resourceType.replaceAll("_", " ")} <span className="font-mono text-xs">{resourceId}</span>
    </>
  );
}

export function AuditView({ fromLink }: { fromLink: string }) {
  const { time_zone: zone } = useClinic();
  const [callIdText, setCallIdText] = useState(fromLink);
  const [actionChoice, setActionChoice] = useState(ALL_ACTIONS);
  const [filter, setFilter] = useState<AuditFilter>(isCallId(fromLink) ? { callId: fromLink } : {});
  const [invalid, setInvalid] = useState(false);
  const errorId = useId();
  const events = useAudit(filter);
  const rows = allRows(events.data);

  const apply = (event: FormEvent) => {
    event.preventDefault();
    const callId = callIdText.trim();
    if (callId !== "" && !isCallId(callId)) {
      setInvalid(true);
      return;
    }
    setInvalid(false);
    setFilter({
      callId: callId === "" ? undefined : callId,
      action: actionChoice === ALL_ACTIONS ? undefined : actionChoice,
    });
  };

  const clear = () => {
    setCallIdText("");
    setActionChoice(ALL_ACTIONS);
    setInvalid(false);
    setFilter({});
  };

  let body;
  if (events.isPending) {
    body = <TableSkeleton label="Loading the audit log" />;
  } else if (events.isError) {
    body = <ErrorState error={events.error} onRetry={() => void events.refetch()} />;
  } else if (rows.length === 0) {
    body = (
      <EmptyState icon={ShieldQuestion} title="No events match">
        No event has this call id and action. Clear the filters to see the whole log.
      </EmptyState>
    );
  } else {
    body = (
      <>
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="pl-5 text-right">Event</TableHead>
              <TableHead>Time</TableHead>
              <TableHead>Actor</TableHead>
              <TableHead>Action</TableHead>
              <TableHead>Record</TableHead>
              <TableHead>Outcome</TableHead>
              <TableHead>Reason</TableHead>
              <TableHead className="pr-5">Call</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((event) => (
              <TableRow key={event.seq}>
                <TableCell className="tabular pl-5 text-right">{event.seq}</TableCell>
                <TableCell className="tabular">{clinicDateTime(event.ts, zone)}</TableCell>
                <TableCell>
                  {event.actor.startsWith("admin:") ? (
                    <span className="rounded-full bg-secondary px-2 py-0.5 text-xs font-medium text-secondary-foreground">
                      {event.actor}
                    </span>
                  ) : (
                    event.actor
                  )}
                </TableCell>
                <TableCell className="font-mono text-xs">{event.action}</TableCell>
                <TableCell>{record(event.resource_type, event.resource_id)}</TableCell>
                <TableCell>
                  <OutcomeBadge outcome={event.outcome} />
                </TableCell>
                <TableCell className="font-mono text-xs">{event.reason ?? "–"}</TableCell>
                <TableCell className="pr-5">
                  {event.call_id ? <CallLink callId={event.call_id} /> : "–"}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        <ShowMore
          hasMore={events.hasNextPage}
          loading={events.isFetchingNextPage}
          onClick={() => void events.fetchNextPage()}
          label="Show older events"
          shown={`Showing ${plural(rows.length, "event")}`}
        />
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Audit log"
        description="Who did what, to which record, and how it ended. The log holds action names and outcomes only, never what was said or any patient detail."
      />
      <ChainPanel />
      <Section title="Events" description="Newest first. Opening this page adds an event of its own.">
        <form onSubmit={apply} className="flex flex-col gap-3 border-b px-5 py-4 md:flex-row md:items-end">
          <div className="flex flex-col gap-1.5 md:w-80">
            <Label htmlFor="audit-call-id">Call id</Label>
            <Input
              id="audit-call-id"
              value={callIdText}
              onChange={(event) => setCallIdText(event.target.value)}
              placeholder="The full id, from Call history"
              autoComplete="off"
              spellCheck={false}
              className="h-10 bg-card font-mono text-xs placeholder:font-sans placeholder:text-sm"
              aria-invalid={invalid}
              aria-describedby={invalid ? errorId : undefined}
            />
          </div>
          <div className="flex flex-col gap-1.5 md:w-64">
            <Label htmlFor="audit-action">Action</Label>
            <Select value={actionChoice} onValueChange={setActionChoice}>
              <SelectTrigger id="audit-action" className="w-full bg-card data-[size=default]:h-10">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL_ACTIONS}>All actions</SelectItem>
                {AUDIT_ACTIONS.map((action) => (
                  <SelectItem key={action} value={action} className="font-mono text-xs">
                    {action}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex gap-2">
            <Button type="submit" variant="outline" className="h-10 px-4">
              Apply
            </Button>
            <Button type="button" variant="ghost" className="h-10 px-4 text-primary" onClick={clear}>
              Clear
            </Button>
          </div>
        </form>
        {invalid ? (
          <p id={errorId} role="alert" className="border-b px-5 py-2 text-sm text-destructive">
            That is not a call id. A call id is 32 letters and digits, as shown on Call history.
          </p>
        ) : null}
        {body}
      </Section>
    </>
  );
}
