import type { EndReason, Phase, RequestType, Stage, TurnKind } from "@/lib/api/schemas";

// Plain words for the registry names the API returns. The audit log's own action names are shown
// as they are, in a monospace face, since that is what the log holds.

export const PHASE_LABEL: Record<Phase, string> = {
  greeting: "Greeting",
  intake: "Intake",
  serving: "Serving",
  booking: "Booking",
  emergency: "Emergency",
  handoff: "Hand-off",
  closing: "Closing",
  ended: "Ended",
};

export const END_REASON_LABEL: Record<EndReason, string> = {
  completed: "Completed",
  emergency: "Emergency",
  transferred: "Transferred",
  caller_hung_up: "Hung up",
  idle_timeout: "Idle",
  max_duration: "Maximum length",
  error: "Error",
};

export const REQUEST_TYPE_LABEL: Record<RequestType, string> = {
  refill: "Refill",
  booking: "Booking",
  callback: "Callback",
  doctor_message: "Doctor message",
  transfer: "Transfer",
};

export const STAGE_LABEL: Record<Stage, string> = {
  vad_ms: "Silence wait",
  stt_ms: "Transcript",
  first_token_ms: "First token",
  last_token_ms: "Last token",
  first_audio_ms: "First audio",
  total_ms: "Whole turn",
};

export const STAGE_HINT: Record<Stage, string> = {
  vad_ms: "How long the agent waits for silence before it answers",
  stt_ms: "From the end of speech to the transcript",
  first_token_ms: "The model's first word of text",
  last_token_ms: "The model's last word of text",
  first_audio_ms: "From the end of speech to the first sound the caller hears",
  total_ms: "From the end of speech to the end of the answer",
};

export const TURN_KIND_LABEL: Record<TurnKind, string> = {
  plain: "Plain answer",
  tool: "With a tool",
  scripted: "Scripted line",
};

export const GUARD_RULE_LABEL: Record<string, string> = {
  medical_advice: "Medical advice",
  refill_promise: "Refill promise",
  phi_outside_gate: "Patient details before verification",
  ssn_last4: "Last four SSN digits",
  ssn_shape: "Nine-digit number",
};

export const EMERGENCY_TRIGGER_LABEL: Record<string, string> = {
  guard: "Phrase list",
  model: "The language model",
};

export const TRANSFER_REASON_LABEL: Record<string, string> = {
  caller_request: "Caller asked for staff",
  frustration: "Caller frustrated",
  verification_locked: "Verification locked",
  other: "Other",
};

// The audit log's actions (Action in app/audit/log.py), which the API accepts as a filter.
export const AUDIT_ACTIONS = [
  "call_started",
  "call_ended",
  "call_closed",
  "tool_called",
  "tool_refused",
  "tool_failed",
  "identity_claimed",
  "identity_verified",
  "identity_refused",
  "identity_locked",
  "appointment_read",
  "refill_requested",
  "refill_previewed",
  "refill_not_found",
  "lab_status_read",
  "balance_read",
  "doctor_message_sent",
  "doctor_message_previewed",
  "message_taken",
  "message_previewed",
  "transfer_requested",
  "transfer_previewed",
  "emergency_detected",
  "new_patient_started",
  "new_patient_details_collected",
  "slots_offered",
  "appointment_booked",
  "new_patient_booked",
  "booking_previewed",
  "slot_taken",
  "booking_repeated",
  "guard_block",
  "admin_read_requests",
  "admin_read_messages",
  "admin_read_audit",
  "admin_verify_audit",
  "admin_read_calls",
  "admin_read_call",
  "admin_read_live_calls",
  "admin_read_call_turns",
  "admin_read_appointments",
  "admin_read_slots",
  "admin_read_guardrails",
  "admin_read_metrics",
  "admin_read_queue",
  "admin_read_call_detail",
  "admin_signed_in",
  "admin_signed_out",
] as const;

// The periods Guardrails and Metrics summarise: days at the clinic, today included.
export const PERIODS = [
  { value: "1", label: "Today" },
  { value: "7", label: "7 days" },
  { value: "30", label: "30 days" },
] as const;
export type Period = (typeof PERIODS)[number]["value"];

export function endReasonLabel(reason: string | null): string {
  if (reason === null) {
    return "Not recorded";
  }
  return END_REASON_LABEL[reason as EndReason] ?? reason;
}
