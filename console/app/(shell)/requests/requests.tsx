"use client";

import {
  ArrowUpRight,
  CalendarDays,
  Check,
  Eye,
  EyeOff,
  Inbox,
  MessageSquare,
  Phone,
  Pill,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { CallLink } from "@/components/call-link";
import { useClinic } from "@/components/clinic";
import { ConfirmDialog, writeFailure } from "@/components/confirm-dialog";
import { FilterPills } from "@/components/filter-pills";
import { MaskedText } from "@/components/masked-text";
import { PageHeader, Section } from "@/components/page-header";
import { StatusPill } from "@/components/status-badge";
import { EmptyState, ErrorState, ShowMore, TableSkeleton } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { allRows, useApi, useMarkHandled, useQueue } from "@/lib/api/hooks";
import {
  callbackDetailsSchema,
  REQUEST_TYPES,
  type CallbackDetails,
  type HandledMark,
  type QueueItem,
  type QueueState,
  type RequestType,
} from "@/lib/api/schemas";
import { clinicDateTime, phoneForReading, plural } from "@/lib/format";
import { REQUEST_TYPE_LABEL } from "@/lib/labels";

type Filter = RequestType | "all";

const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "All types" },
  ...REQUEST_TYPES.map((type) => ({ value: type, label: REQUEST_TYPE_LABEL[type] })),
];

const STATES: { value: QueueState; label: string }[] = [
  { value: "todo", label: "To do" },
  { value: "handled", label: "Handled" },
  { value: "all", label: "All" },
];

const TYPE_ICON: Record<RequestType, LucideIcon> = {
  refill: Pill,
  booking: CalendarDays,
  callback: Phone,
  doctor_message: MessageSquare,
  transfer: ArrowUpRight,
};

// How long a callback's details stay on screen at most.
export const REVEAL_SECONDS = 60;

function Summary({ request }: { request: QueueItem }) {
  const notes = [
    ...request.callbacks.map((number) => `Call back ${number}`),
    ...(request.after_hours ? ["left after hours"] : []),
    ...(request.type === "callback" || request.type === "doctor_message" ? ["caller's name hidden"] : []),
  ];
  return (
    <div className="max-w-md min-w-56 whitespace-normal">
      <p className="leading-snug">
        <MaskedText text={request.summary} />
      </p>
      {notes.length > 0 ? <p className="mt-0.5 text-xs text-muted-foreground">{notes.join(" · ")}</p> : null}
    </div>
  );
}

// A row's two places focus can be sent to: its mark ("Mark handled", "Marking…" or the handled
// text) and its callback details (the button, "Showing…" or the details).
type Slot = "mark" | "reveal";

interface FocusRequest {
  id: number;
  slot: Slot;
}

function slotName(slot: Slot, id: number): string {
  return `${slot}-${id}`;
}

// Whether focus is on what fills that slot now (or nowhere): then it follows when the slot's
// content is replaced, as it would otherwise be lost.
function focusIn(slot: Slot, id: number): boolean {
  const active = document.activeElement;
  return (
    active === null ||
    active === document.body ||
    active.closest(`[data-focus-slot="${slotName(slot, id)}"]`) !== null
  );
}

// One row's state, as the page holds it: its handled mark (from the list or the API's answer to
// "Mark handled"), whether a write for it is on its way, what went wrong with the last one, and,
// for at most one row at a time, the callback details the API showed.
interface RowState {
  mark: HandledMark | null;
  marking: boolean;
  revealing: boolean;
  revealed: CallbackDetails | null;
  failure: string | null;
  focus: FocusRequest | null;
}

const pending = "inline-flex h-8 items-center px-2.5 text-sm text-muted-foreground outline-none";

// What staff can do with a request, written once for the stacked rows and the table (one of the
// two is hidden by the layout; focus sent to a hidden one goes nowhere).
function RequestActions({
  request,
  row,
  zone,
  className,
  onMark,
  onReveal,
  onHide,
}: {
  request: QueueItem;
  row: RowState;
  zone: string;
  className: string;
  onMark: () => void;
  onReveal: () => void;
  onHide: () => void;
}) {
  const markSlot = useRef<HTMLElement | null>(null);
  const revealSlot = useRef<HTMLElement | null>(null);
  const { focus } = row;
  useEffect(() => {
    if (focus === null) return;
    (focus.slot === "mark" ? markSlot : revealSlot).current?.focus();
  }, [focus]);
  const intoMark = (element: HTMLElement | null) => {
    markSlot.current = element;
  };
  const intoReveal = (element: HTMLElement | null) => {
    revealSlot.current = element;
  };
  const mark = slotName("mark", request.id);
  const reveal = slotName("reveal", request.id);

  return (
    <div className={className} data-request={request.id}>
      {row.mark ? (
        <p
          ref={intoMark}
          tabIndex={-1}
          data-focus-slot={mark}
          className="inline-flex items-center gap-1.5 text-sm outline-none"
        >
          <StatusPill tone="good" icon={Check}>
            Handled
          </StatusPill>
          <span className="text-xs text-muted-foreground">
            by {row.mark.by} at {clinicDateTime(row.mark.at, zone)}
          </span>
        </p>
      ) : row.marking ? (
        <span ref={intoMark} tabIndex={-1} data-focus-slot={mark} className={pending}>
          Marking…
        </span>
      ) : (
        <Button ref={intoMark} data-focus-slot={mark} variant="outline" size="sm" onClick={onMark}>
          Mark handled
        </Button>
      )}
      {request.callbacks.length > 0 ? (
        row.revealed ? (
          // Keyed apart from the button's wrapper, so hiding them replaces the element that had
          // focus instead of reusing it.
          <div
            key="details"
            ref={intoReveal}
            tabIndex={-1}
            role="group"
            aria-label="Callback details"
            data-focus-slot={reveal}
            className="w-full max-w-xs rounded-md border border-primary/30 bg-secondary/60 px-3 py-2 text-left text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <ul className="space-y-1">
              {row.revealed.map((entry, index) => (
                <li key={index}>
                  <span className="block font-medium">{entry.name ?? "No name given"}</span>
                  <span className="block font-mono text-xs">{phoneForReading(entry.callback_number)}</span>
                </li>
              ))}
            </ul>
            <div className="mt-2 flex items-center justify-between gap-2">
              <span className="text-xs text-muted-foreground">Hidden again within {REVEAL_SECONDS} s</span>
              <Button variant="ghost" size="sm" onClick={onHide}>
                <EyeOff aria-hidden />
                Hide
              </Button>
            </div>
          </div>
        ) : (
          <div key="ask" className="flex flex-col gap-0.5 sm:items-end">
            {row.revealing ? (
              <span ref={intoReveal} tabIndex={-1} data-focus-slot={reveal} className={pending}>
                Showing…
              </span>
            ) : (
              <Button ref={intoReveal} data-focus-slot={reveal} variant="ghost" size="sm" onClick={onReveal}>
                <Eye aria-hidden />
                Show callback details
              </Button>
            )}
            <span className="text-xs text-muted-foreground">Showing this is recorded in the audit log.</span>
          </div>
        )
      ) : null}
      {row.failure ? (
        <p role="alert" className="text-xs text-destructive">
          {row.failure}
        </p>
      ) : null}
    </div>
  );
}

interface Revealed {
  id: number;
  callbacks: CallbackDetails;
  // The list it was shown over: any other list (a refetch, another filter) clears it.
  list: string;
}

export function RequestsView() {
  const { time_zone: zone } = useClinic();
  const api = useApi();
  const [filter, setFilter] = useState<Filter>("all");
  const [state, setState] = useState<QueueState>("todo");
  const queue = useQueue(filter === "all" ? undefined : filter, state);
  const rows = allRows(queue.data);
  const markHandled = useMarkHandled();

  const [marks, setMarks] = useState<Record<number, HandledMark>>({});
  // The rows whose "Mark handled" is on its way, each until its own answer comes.
  const [marking, setMarking] = useState<ReadonlySet<number>>(new Set());
  const [failures, setFailures] = useState<Record<number, string>>({});
  const [confirming, setConfirming] = useState<number | null>(null);
  // The rows whose details were asked for and not answered yet.
  const [revealing, setRevealing] = useState<ReadonlySet<number>>(new Set());
  const [revealed, setRevealed] = useState<Revealed | null>(null);
  const [focus, setFocus] = useState<FocusRequest | null>(null);
  // Read by assistive technology; it says only that details were shown or hidden.
  const [announcement, setAnnouncement] = useState("");
  // The one reveal whose answer is still wanted: asking for another row's details or a new list
  // makes any answer on its way unwanted (leaving the page drops it with the page's state).
  const wanted = useRef(0);

  // Shown details belong to the list they were shown over; when the list is read again, or
  // another one is shown, they go.
  const list = `${filter}|${state}|${queue.dataUpdatedAt}`;
  if (revealed !== null && revealed.list !== list) {
    setRevealed(null);
    setAnnouncement("Callback details hidden");
  }
  useEffect(() => {
    wanted.current += 1;
  }, [list]);

  const hide = (id: number, refocus: boolean) => {
    setRevealed(null);
    setAnnouncement("Callback details hidden");
    if (refocus) setFocus({ id, slot: "reveal" });
  };

  // And they go after REVEAL_SECONDS in any case; focus inside them goes back to the row's button.
  useEffect(() => {
    if (revealed === null) return;
    const { id } = revealed;
    const timer = setTimeout(() => {
      const refocus = focusIn("reveal", id);
      setRevealed(null);
      setAnnouncement("Callback details hidden");
      if (refocus) setFocus({ id, slot: "reveal" });
    }, REVEAL_SECONDS * 1000);
    return () => clearTimeout(timer);
  }, [revealed]);

  const without = (set: ReadonlySet<number>, id: number) => new Set([...set].filter((each) => each !== id));
  const fail = (id: number, message: string) => setFailures((current) => ({ ...current, [id]: message }));
  const clearFailure = (id: number) =>
    setFailures((current) => {
      const rest = { ...current };
      delete rest[id];
      return rest;
    });

  const confirmMark = async () => {
    if (confirming === null) return;
    const id = confirming;
    setConfirming(null);
    clearFailure(id);
    setMarking((current) => new Set(current).add(id));
    setFocus({ id, slot: "mark" });
    // Each row's write is awaited on its own, so two on their way at once each get their answer.
    try {
      const { answer } = await markHandled.mutateAsync(id);
      // The row changes only once the API has answered; a mark it already had reads the same.
      setMarks((current) => ({ ...current, [id]: answer.handled }));
    } catch (error) {
      fail(id, writeFailure(error, "Could not mark it handled. Nothing changed."));
    }
    const refocus = focusIn("mark", id);
    setMarking((current) => without(current, id));
    if (refocus) setFocus({ id, slot: "mark" });
  };

  const reveal = async (id: number) => {
    const ask = ++wanted.current;
    clearFailure(id);
    if (revealed !== null) hide(revealed.id, false);
    setRevealing((current) => new Set(current).add(id));
    setFocus({ id, slot: "reveal" });
    const shownOver = list;
    try {
      // A plain call through the fetch wrapper, not a query or a mutation: the answer goes into
      // this page's state and nowhere else, and only while it is still wanted.
      const { answer } = await api.post(`/queue/requests/${id}/callback`, callbackDetailsSchema, {});
      if (ask === wanted.current) {
        setRevealed({ id, callbacks: answer.callbacks, list: shownOver });
        setAnnouncement("Callback details shown");
      }
    } catch (error) {
      fail(id, writeFailure(error, "Could not show the details."));
    }
    const refocus = focusIn("reveal", id);
    setRevealing((current) => without(current, id));
    if (refocus) setFocus({ id, slot: "reveal" });
  };

  const rowState = (request: QueueItem): RowState => ({
    mark: marks[request.id] ?? request.handled,
    marking: marking.has(request.id),
    revealing: revealing.has(request.id),
    revealed: revealed !== null && revealed.id === request.id ? revealed.callbacks : null,
    failure: failures[request.id] ?? null,
    focus: focus !== null && focus.id === request.id ? focus : null,
  });

  const actions = (request: QueueItem, className: string) => (
    <RequestActions
      request={request}
      row={rowState(request)}
      zone={zone}
      className={className}
      onMark={() => setConfirming(request.id)}
      onReveal={() => void reveal(request.id)}
      onHide={() => hide(request.id, true)}
    />
  );

  let body;
  if (queue.isPending) {
    body = <TableSkeleton label="Loading the requests" />;
  } else if (queue.isError) {
    body = <ErrorState error={queue.error} onRetry={() => void queue.refetch()} />;
  } else if (rows.length === 0) {
    body = (
      <EmptyState
        icon={Inbox}
        title={
          state === "todo"
            ? "Nothing to do"
            : state === "handled"
              ? "Nothing handled yet"
              : filter === "all"
                ? "No requests yet"
                : `No ${REQUEST_TYPE_LABEL[filter].toLowerCase()} requests`
        }
      >
        Refills, bookings, callbacks, messages for a doctor and transfers appear here as the agent takes them.
      </EmptyState>
    );
  } else {
    body = (
      <>
        <ul className="divide-y sm:hidden">
          {rows.map((request) => (
            <li key={request.id} className="space-y-2 px-5 py-4">
              <div className="flex items-center justify-between gap-3">
                <StatusPill tone="neutral" icon={TYPE_ICON[request.type]}>
                  {REQUEST_TYPE_LABEL[request.type]}
                </StatusPill>
                <span className="tabular text-sm text-muted-foreground">
                  {clinicDateTime(request.created_at, zone)}
                </span>
              </div>
              <Summary request={request} />
              <p className="flex flex-wrap items-center gap-x-2 text-sm">
                {request.patient_id === null ? (
                  <span className="text-muted-foreground">Not verified</span>
                ) : (
                  <span className="font-mono text-xs">{request.patient_id}</span>
                )}
                <span className="text-muted-foreground">· call</span>
                <CallLink callId={request.call_id} />
              </p>
              {actions(request, "flex flex-col items-start gap-2")}
            </li>
          ))}
        </ul>
        <Table className="hidden sm:table">
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="pl-5">Received</TableHead>
              <TableHead>Type</TableHead>
              <TableHead>Patient</TableHead>
              <TableHead>Summary</TableHead>
              <TableHead>Call</TableHead>
              <TableHead className="pr-5">
                <span className="sr-only">Actions</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((request) => {
              const Icon = TYPE_ICON[request.type];
              return (
                <TableRow key={request.id} className="align-top">
                  <TableCell className="tabular pt-3 pl-5 align-top">
                    {clinicDateTime(request.created_at, zone)}
                  </TableCell>
                  <TableCell className="pt-3 align-top">
                    <StatusPill tone="neutral" icon={Icon}>
                      {REQUEST_TYPE_LABEL[request.type]}
                    </StatusPill>
                  </TableCell>
                  <TableCell className="pt-3 align-top">
                    {request.patient_id === null ? (
                      <span className="text-muted-foreground">Not verified</span>
                    ) : (
                      <span className="font-mono text-xs">{request.patient_id}</span>
                    )}
                  </TableCell>
                  <TableCell className="pt-3 align-top">
                    <Summary request={request} />
                  </TableCell>
                  <TableCell className="pt-3 align-top">
                    <CallLink callId={request.call_id} />
                  </TableCell>
                  <TableCell className="pr-5 align-top">
                    {actions(request, "flex flex-col items-end gap-1.5 text-right")}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
        <ShowMore
          hasMore={queue.hasNextPage}
          loading={queue.isFetchingNextPage}
          onClick={() => void queue.fetchNextPage()}
          label="Show older requests"
          shown={`Showing ${plural(rows.length, "request")}`}
        />
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Requests"
        description="Everything the agent took for staff to act on: refills, bookings, callbacks, messages for a doctor and transfers."
      />
      <Section
        title="Queue"
        description="Newest first. Patients appear as ids. A callback number shows its last four digits until you ask to see it, and that is recorded in the audit log."
      >
        <div className="flex flex-col gap-3 border-b px-5 py-4 lg:flex-row lg:items-start lg:justify-between">
          <FilterPills label="Type of request" value={filter} options={FILTERS} onChange={setFilter} />
          <div className="shrink-0">
            <FilterPills label="Handled or not" value={state} options={STATES} onChange={setState} />
          </div>
        </div>
        {body}
      </Section>
      <ConfirmDialog
        open={confirming !== null}
        onOpenChange={(open) => {
          if (!open && confirming !== null) {
            // The row's mark takes focus back: its button, or what replaces it once confirmed.
            setFocus({ id: confirming, slot: "mark" });
            setConfirming(null);
          }
        }}
        title="Mark this request as handled?"
        description="This cannot be undone here. The mark, with your name and the time, is recorded in the audit log."
        action="Mark handled"
        onConfirm={() => void confirmMark()}
        returnFocus={false}
      />
      <p id="reveal-status" role="status" aria-live="polite" className="sr-only">
        {announcement}
      </p>
    </>
  );
}
