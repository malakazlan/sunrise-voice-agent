"use client";

import {
  ArrowLeft,
  ArrowUpRight,
  Ban,
  CalendarDays,
  Check,
  Circle,
  CircleCheck,
  EyeOff,
  FileQuestion,
  Lock,
  Phone,
  Pill,
  ShieldAlert,
  TriangleAlert,
  UserRound,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";

import { useClinic } from "@/components/clinic";
import { MaskedText } from "@/components/masked-text";
import { Section, StatCard } from "@/components/page-header";
import { EndReasonBadge, OutcomeBadge, PhaseBadge } from "@/components/status-badge";
import { BlockSkeleton, EmptyState, ErrorState, ShowMore, TableSkeleton } from "@/components/states";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ApiError } from "@/lib/api/client";
import { allRows, useCallDetail, useCallTurns } from "@/lib/api/hooks";
import type { AuditEvent, CallDetail } from "@/lib/api/schemas";
import {
  clinicLongDate,
  clinicTimeSeconds,
  duration,
  plural,
  secondsBetween,
  secondsFigure,
  shortId,
} from "@/lib/format";
import { cn } from "@/lib/utils";

const WITHHELD = /^\((?:emergency )?turn withheld\)$/;

const EVENT_ICON: Record<string, LucideIcon> = {
  call_started: Phone,
  identity_claimed: UserRound,
  identity_verified: Check,
  identity_refused: Lock,
  identity_locked: Lock,
  appointment_read: CalendarDays,
  refill_requested: Pill,
  emergency_detected: TriangleAlert,
  guard_block: ShieldAlert,
  tool_refused: Ban,
  transfer_requested: ArrowUpRight,
  call_closed: CircleCheck,
  call_ended: CircleCheck,
};

// The stored transcript is one line per message, "caller: …" or "agent: …"; the caller's lines
// are numbered as its turns.
function transcriptLines(transcript: string): { speaker: string; text: string; turn: number | null }[] {
  const lines = transcript.split("\n").filter((line) => line.trim() !== "");
  return lines.map((line, index) => {
    const [speaker, ...rest] = line.split(": ");
    const turn =
      speaker === "caller"
        ? lines.slice(0, index + 1).filter((earlier) => earlier.startsWith("caller: ")).length
        : null;
    return { speaker, text: rest.join(": "), turn };
  });
}

// Why the API withheld a transcript, in words; a reason it does not know is shown as it came.
const WITHHELD_BECAUSE: Record<string, string> = {
  names_not_masked:
    "The second masking pass, which hides names, did not run when this call was stored. The transcript is not shown until it has.",
};

function Transcript({ call }: { call: CallDetail["call"] }) {
  if (call.transcript === null) {
    const reason = call.transcript_withheld;
    return (
      <div className="flex gap-3 px-5 py-5">
        <EyeOff className="mt-0.5 size-5 shrink-0 text-muted-foreground" aria-hidden />
        <div>
          <p className="font-medium">Transcript withheld</p>
          <p className="mt-1 text-sm text-muted-foreground">
            {reason === null
              ? "The admin API did not send this call's transcript."
              : (WITHHELD_BECAUSE[reason] ?? `The admin API withheld it: ${reason}.`)}
          </p>
        </div>
      </div>
    );
  }
  return (
    <>
      <ol className="divide-y">
        {transcriptLines(call.transcript).map(({ speaker, text, turn }, index) => {
          const caller = speaker === "caller";
          const withheld = WITHHELD.test(text);
          return (
            <li
              key={index}
              className={cn(
                "grid grid-cols-[2rem_4rem_1fr] gap-x-3 px-5 py-2.5 text-sm sm:grid-cols-[2.5rem_4.5rem_1fr]",
                caller && "bg-muted/40",
              )}
            >
              <span className="tabular text-muted-foreground">{turn ?? ""}</span>
              <span className={cn("font-medium", caller ? "text-foreground" : "text-primary")}>
                {caller ? "Caller" : speaker === "agent" ? "Agent" : speaker}
              </span>
              {withheld ? (
                <span className="inline-flex items-center gap-1.5 font-mono text-xs text-muted-foreground">
                  <EyeOff className="size-3.5" aria-hidden />
                  {text}
                </span>
              ) : (
                <span className="leading-relaxed text-pretty">
                  <MaskedText text={text} />
                </span>
              )}
            </li>
          );
        })}
      </ol>
      {call.transcript_truncated ? (
        <p className="border-t px-5 py-3 text-sm text-muted-foreground">
          The transcript was cut at its longest stored length; the rest of the call is not shown.
        </p>
      ) : null}
    </>
  );
}

function Timeline({ events, zone }: { events: AuditEvent[]; zone: string }) {
  if (events.length === 0) {
    return <p className="px-5 py-5 text-sm text-muted-foreground">No events were recorded for this call.</p>;
  }
  return (
    <ol className="relative px-5 py-4">
      {events.map((event, index) => {
        const Icon = EVENT_ICON[event.action] ?? Circle;
        const last = index === events.length - 1;
        return (
          <li key={event.seq} className="relative flex gap-3 pb-4 last:pb-0">
            {!last ? (
              <span className="absolute top-8 bottom-0 left-[0.9375rem] w-px bg-border" aria-hidden />
            ) : null}
            <span
              className={cn(
                "relative flex size-8 shrink-0 items-center justify-center rounded-full ring-1",
                event.action === "emergency_detected"
                  ? "bg-emergency-soft text-emergency ring-emergency/30"
                  : "bg-secondary text-secondary-foreground ring-primary/15",
              )}
            >
              <Icon className="size-4" aria-hidden />
            </span>
            <div className="min-w-0 pt-1">
              <p className="font-mono text-[0.8rem] font-medium break-all">{event.action}</p>
              <p className="mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-muted-foreground">
                <span className="tabular">{clinicTimeSeconds(event.ts, zone)}</span>
                {event.resource_type === "patient" && event.resource_id ? (
                  <span>
                    · patient <span className="font-mono">{event.resource_id}</span>
                  </span>
                ) : null}
                {event.reason ? (
                  <span>
                    · reason <span className="font-mono">{event.reason}</span>
                  </span>
                ) : null}
                <span>·</span>
                <OutcomeBadge outcome={event.outcome} />
                <span>· event {event.seq}</span>
              </p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function Turns({ callId, zone }: { callId: string; zone: string }) {
  const turns = useCallTurns(callId);
  const rows = allRows(turns.data);
  if (turns.isPending) return <TableSkeleton label="Loading the turns" rows={4} />;
  if (turns.isError) return <ErrorState error={turns.error} onRetry={() => void turns.refetch()} />;
  if (rows.length === 0) {
    return (
      <EmptyState icon={FileQuestion} title="No turns recorded">
        The caller did not speak in this call, or its turns were not recorded.
      </EmptyState>
    );
  }
  const seconds = (value: number | null) => (value === null ? "–" : secondsFigure(value));
  return (
    <>
      <Table>
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            <TableHead className="pl-5 text-right">Turn</TableHead>
            <TableHead>At</TableHead>
            <TableHead>Phase after</TableHead>
            <TableHead>Input guard</TableHead>
            <TableHead>Last tool</TableHead>
            <TableHead className="text-right">Silence wait</TableHead>
            <TableHead className="text-right">Transcript</TableHead>
            <TableHead className="text-right">First token</TableHead>
            <TableHead className="text-right">First audio</TableHead>
            <TableHead className="text-right">Whole turn</TableHead>
            <TableHead className="pr-5 text-right">Recording cost</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((turn) => (
            <TableRow key={turn.turn}>
              <TableCell className="tabular pl-5 text-right">{turn.turn}</TableCell>
              <TableCell className="tabular">{clinicTimeSeconds(turn.at, zone)}</TableCell>
              <TableCell>
                <PhaseBadge phase={turn.phase} />
              </TableCell>
              <TableCell className="font-mono text-xs">{turn.guard_action}</TableCell>
              <TableCell className="font-mono text-xs">
                {turn.last_tool ?? "–"}
                {turn.tool_rounds > 1 ? ` ×${turn.tool_rounds}` : ""}
              </TableCell>
              <TableCell className="tabular text-right">{seconds(turn.vad_ms)}</TableCell>
              <TableCell className="tabular text-right">{seconds(turn.stt_ms)}</TableCell>
              <TableCell className="tabular text-right">{seconds(turn.first_token_ms)}</TableCell>
              <TableCell className="tabular text-right font-semibold">
                {seconds(turn.first_audio_ms)}
              </TableCell>
              <TableCell className="tabular text-right">{seconds(turn.total_ms)}</TableCell>
              <TableCell className="tabular pr-5 text-right">{turn.record_us} µs</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      {turns.hasNextPage ? (
        <ShowMore
          hasMore
          loading={turns.isFetchingNextPage}
          onClick={() => void turns.fetchNextPage()}
          label="Show later turns"
          shown={`Showing ${plural(rows.length, "turn")}`}
        />
      ) : null}
    </>
  );
}

function BackLink() {
  return (
    <Link
      href="/calls"
      className="mb-4 inline-flex min-h-9 items-center gap-1.5 rounded-md text-sm font-medium text-primary hover:underline"
    >
      <ArrowLeft className="size-4" aria-hidden />
      Back to call history
    </Link>
  );
}

export function CallDetailView({ callId }: { callId: string }) {
  const { time_zone: zone } = useClinic();
  const detail = useCallDetail(callId);

  if (detail.isPending) {
    return (
      <>
        <BackLink />
        <BlockSkeleton className="mb-6 h-12 w-72" label="Loading the call" />
        <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
          {[0, 1, 2, 3].map((index) => (
            <BlockSkeleton key={index} className="h-28 w-full" label="Loading" />
          ))}
        </div>
      </>
    );
  }
  if (detail.isError) {
    const missing = detail.error instanceof ApiError && detail.error.kind === "not_found";
    return (
      <>
        <BackLink />
        {missing ? (
          <EmptyState icon={FileQuestion} title="No call with this id">
            The call may still be in progress, or the id is wrong. A call is stored once it has ended.
          </EmptyState>
        ) : (
          <ErrorState error={detail.error} onRetry={() => void detail.refetch()} />
        )}
      </>
    );
  }

  const { call, events } = detail.data;
  return (
    <>
      <BackLink />
      <header className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="font-heading text-2xl font-semibold tracking-tight sm:text-[1.75rem]">
            Call{" "}
            <span className="font-mono text-xl" title={call.call_id}>
              {shortId(call.call_id)}…
            </span>
          </h1>
          <p className="mt-1 text-muted-foreground">
            {clinicLongDate(call.started_at, zone)} · office {call.office_open ? "open" : "closed"}
          </p>
        </div>
        <div className="flex items-center gap-2 text-sm">
          <span className="text-muted-foreground">Ended:</span>
          <EndReasonBadge reason={call.end_reason} />
        </div>
      </header>

      <div className="mb-6 grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
        <StatCard
          label="Started"
          value={clinicTimeSeconds(call.started_at, zone)}
          note={`Ended ${clinicTimeSeconds(call.ended_at, zone)}`}
        />
        <StatCard
          label="Length"
          value={duration(secondsBetween(call.started_at, call.ended_at))}
          note={plural(call.turn_count, "caller turn")}
        />
        <StatCard
          label="Caller"
          value={
            call.verified && call.patient_id !== null ? (
              <>
                Verified · <span className="font-mono text-xl">{call.patient_id}</span>
              </>
            ) : (
              "Not verified"
            )
          }
          note={call.office_open ? "Called during office hours" : "Called after hours"}
        />
        <StatCard
          label="Masking"
          value={call.names_masked ? "Both passes ran" : "Second pass did not run"}
          note={
            call.names_masked
              ? "Pass 1: dates, phone numbers, SSN digits. Pass 2: names."
              : "Names may not be hidden, so the transcript is withheld."
          }
        />
      </div>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <Section
          title="Transcript (masked)"
          description="Stored after the call ended. Names, dates of birth, phone numbers and SSN digits are masked. Audio is never stored."
        >
          <Transcript call={call} />
        </Section>
        <Section
          title="What happened"
          description="From the audit log for this call: actions and outcomes only, never what was said."
          action={
            <Link
              href={`/audit?call_id=${call.call_id}`}
              className="text-sm font-medium text-primary hover:underline"
            >
              Open in the audit log
            </Link>
          }
        >
          <Timeline events={events} zone={zone} />
        </Section>
      </div>

      <Section
        className="mt-6"
        title="Turn by turn"
        description="Each caller turn's stages in seconds, as the agent measured them; first audio is the caller's wait to hear the answer. No text of the turn is stored."
      >
        <Turns callId={call.call_id} zone={zone} />
      </Section>
    </>
  );
}
