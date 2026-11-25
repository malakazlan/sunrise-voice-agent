import { z } from "zod";

// Every answer of the admin API is checked here before anything renders it. A value of the wrong
// shape, a missing field or an unknown phase, reason or type fails the whole answer, which the
// page shows as an error.

export const CALL_ID = /^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$/;
export function isCallId(value: string): boolean {
  return CALL_ID.test(value);
}

// What the API leaves of a callback number: its last four digits, or nothing of it.
const MASKED_CALLBACK = /^\*\*\*-\*\*\*-(?:[0-9]{4}|\*{4})$/;

const instant = z.iso.datetime({ offset: true });
const count = z.number().int().nonnegative();
const milliseconds = count.nullable();
// A call id the console builds a path or a key from: exactly the API's shape.
const callId = z.string().regex(CALL_ID);
// A call id that is only shown or linked: one odd value must not blank a whole page, so any short
// text is accepted, and one without the shape is shown as text, not as a link (CallLink).
const shownCallId = z.string().min(1).max(64);

export const PHASES = [
  "greeting",
  "intake",
  "serving",
  "booking",
  "emergency",
  "handoff",
  "closing",
  "ended",
] as const;
export const END_REASONS = [
  "completed",
  "emergency",
  "transferred",
  "caller_hung_up",
  "idle_timeout",
  "max_duration",
  "error",
] as const;
export const REQUEST_TYPES = ["refill", "booking", "callback", "doctor_message", "transfer"] as const;
export const OUTCOMES = ["success", "failure", "refused"] as const;
export const STAGES = [
  "vad_ms",
  "stt_ms",
  "first_token_ms",
  "last_token_ms",
  "first_audio_ms",
  "total_ms",
] as const;
export const TURN_KINDS = ["scripted", "plain", "tool"] as const;

export type Phase = (typeof PHASES)[number];
export type EndReason = (typeof END_REASONS)[number];
export type RequestType = (typeof REQUEST_TYPES)[number];
export type Stage = (typeof STAGES)[number];
export type TurnKind = (typeof TURN_KINDS)[number];

export const sessionSchema = z.object({
  user: z.string().min(1),
  csrf_token: z.string().min(1),
  // Seconds left if the session is not used.
  expires_in: count,
  office_open: z.boolean(),
  time_zone: z.string().min(1),
});

export const liveCallSchema = z.object({
  call_id: callId,
  started_at: instant,
  turn: count.nullable(),
  phase: z.enum(PHASES).nullable(),
  verified: z.boolean().nullable(),
  patient_id: z.string().nullable(),
  last_tool: z.string().nullable(),
  vad_ms: milliseconds,
  stt_ms: milliseconds,
  first_token_ms: milliseconds,
  last_token_ms: milliseconds,
  first_audio_ms: milliseconds,
  total_ms: milliseconds,
});
export const liveCallListSchema = z.array(liveCallSchema);

const callFields = {
  id: count,
  call_id: callId,
  started_at: instant,
  ended_at: instant,
  office_open: z.boolean(),
  verified: z.boolean(),
  patient_id: z.string().nullable(),
  end_reason: z.enum(END_REASONS),
  turn_count: count,
};
export const callSchema = z.object(callFields);
export const callsSchema = z.object({ calls: z.array(callSchema) });

export const auditEventSchema = z.object({
  seq: count,
  ts: instant,
  call_id: shownCallId.nullable(),
  actor: z.string(),
  action: z.string(),
  resource_type: z.string(),
  resource_id: z.string().nullable(),
  outcome: z.enum(OUTCOMES),
  reason: z.string().nullable(),
});
export const auditEventsSchema = z.object({ events: z.array(auditEventSchema) });

export const callDetailSchema = z.object({
  call: z.object({
    ...callFields,
    // Null, with the reason in transcript_withheld, when the transcript is not shown (the second
    // masking pass did not run).
    transcript: z.string().nullable(),
    transcript_truncated: z.boolean(),
    names_masked: z.boolean(),
    transcript_withheld: z.string().nullable(),
  }),
  events: z.array(auditEventSchema),
});

export const turnSchema = z.object({
  turn: count,
  at: instant,
  phase: z.enum(PHASES),
  verified: z.boolean(),
  patient_id: z.string().nullable(),
  guard_action: z.enum(["pass", "swallow", "emergency"]),
  last_tool: z.string().nullable(),
  tool_rounds: count,
  vad_ms: milliseconds,
  stt_ms: milliseconds,
  first_token_ms: milliseconds,
  last_token_ms: milliseconds,
  first_audio_ms: milliseconds,
  total_ms: count,
  record_us: count,
});
export const turnsSchema = z.object({ turns: z.array(turnSchema) });

export const queueItemSchema = z.object({
  id: count,
  created_at: instant,
  call_id: shownCallId,
  type: z.enum(REQUEST_TYPES),
  summary: z.string(),
  patient_id: z.string().nullable(),
  callbacks: z.array(z.string().regex(MASKED_CALLBACK)),
  after_hours: z.boolean(),
});
export const queueSchema = z.object({ requests: z.array(queueItemSchema) });

export const appointmentSchema = z.object({
  id: count,
  starts_at: instant,
  starts_at_utc: instant,
  doctor: z.string(),
  patient_id: z.string(),
  new_patient: z.boolean(),
  reason: z.string().nullable(),
  status: z.string(),
  booked_by_call: shownCallId.nullable(),
});
export const appointmentsSchema = z.object({ appointments: z.array(appointmentSchema) });

export const slotSchema = z.object({
  id: count,
  doctor: z.string(),
  starts_at: instant,
  available: z.boolean(),
});
export const slotsSchema = z.object({ slots: z.array(slotSchema), truncated: z.boolean() });

export const guardrailsSchema = z.object({
  days: count,
  since: instant,
  counts: z.array(
    z.object({ day: instant, action: z.string(), reason: z.string().nullable(), count: count }),
  ),
});

export const latencyRowSchema = z.object({
  kind: z.enum(TURN_KINDS),
  turns: count,
  stage: z.enum(STAGES),
  values: count,
  p50: milliseconds,
  p95: milliseconds,
});

export const metricsSchema = z.object({
  days: count,
  since: instant,
  calls_per_day: z.array(z.object({ day: instant, calls: count })),
  ended: z.array(z.object({ reason: z.string().nullable(), calls: count })),
  verified_calls: count,
  verification_attempts: count,
  verification_successes: count,
  latency: z.array(latencyRowSchema),
  record_us_turns: count,
  record_us_p50: count.nullable(),
  turn_rows_dropped: count,
});

export const verificationSchema = z.object({
  ok: z.boolean(),
  checked: count,
  // A break's position, when the check could tell it.
  breaks: z.array(z.object({ seq: count.nullable(), kind: z.string() })),
  truncated: z.boolean(),
});

export type Session = z.infer<typeof sessionSchema>;
export type LiveCall = z.infer<typeof liveCallSchema>;
export type Call = z.infer<typeof callSchema>;
export type AuditEvent = z.infer<typeof auditEventSchema>;
export type CallDetail = z.infer<typeof callDetailSchema>;
export type Turn = z.infer<typeof turnSchema>;
export type QueueItem = z.infer<typeof queueItemSchema>;
export type Appointment = z.infer<typeof appointmentSchema>;
export type Slot = z.infer<typeof slotSchema>;
export type Guardrails = z.infer<typeof guardrailsSchema>;
export type LatencyRow = z.infer<typeof latencyRowSchema>;
export type Metrics = z.infer<typeof metricsSchema>;
export type Verification = z.infer<typeof verificationSchema>;
