# Output guard

Part of the [README](../README.md)'s reference.

Every sentence of the model's own answer is checked in code before it is returned
(`app/guard/output.py`, pure: no database, network or clock). Scripted lines are code's and are
not checked. The rules, in order; the first that matches blocks the sentence:

| Rule | Blocks | Said instead |
|---|---|---|
| `ssn_last4` | the caller's verified last four, said in numerals or number words, as tokens of a run one after another; runs join across spaces, hyphens, dots, commas, brackets, en and em dashes, "and", um and uh; read over the reply so far, so "33. 44." is blocked at its second sentence. A run that reads as a callback number (10 or 11 digits) joined by separators only is a phone number and passes; so does a longer number that holds the digits inside one token | `fallback` |
| `ssn_shape` | a run that reads as nine digits, in any grouping, verified or not, inside letters too; unless those nine are digits the caller said on this call, one after another (their own phone number read back before it is whole) | `fallback` |
| `phi_outside_gate` | before verification or with the office closed: a money amount (`$`, dollars, cents, or balance/owe with a number), a lab status (labs or results with ready, pending, came back, no recent ...), a dosage (a number before mg, mcg, ml, units, tablets, pills, capsules) | `fallback` |
| `medical_advice` | "you should/need to/can take, stop, start, increase, decrease, double, skip" (not "take your insurance card", "stop by"), "stop/start/keep taking", "hold off on taking", "don't take", "it's probably" (not "probably best", "probably in your spam folder"), "it sounds like you have", "that sounds like a", "it could be a" (not "a few days"), "you may/might/probably/likely have" (not "have to", "have received", "a copay", "a message" ...), "that's normal" (not "normal for lab results"), "nothing to worry about", "I recommend/suggest taking/trying" (not "trying to", "trying our"), "I'd recommend you take", "it's fine/okay/safe to take or skip", "safe to take", "side effects of" | `advice_refusal` |
| `refill_promise` | refill, prescription or medication in the same sentence as approved, will be ready, is/are ready (not "ready to send"), is all set, has been filled or refilled; not when whether, if, once, until, when, after, can't, cannot, don't know, not, or must/need to be comes before the promise word | `refill_no_promise` |

- `advice_refusal`: "I'm not able to give medical advice. I can send a message to your doctor
  instead." `refill_no_promise`: "I'm not able to confirm that a refill is approved. I can send
  the request to our team." Neither claims that anything was done.
- Digits are read by the mask module (`app/guard/mask.py`), the one owner of the SSN patterns.
  The output rules read numerals and number words, not the homophones the input guard also
  reads ("to", "for", "won", "ate"), so "from ten thirty to eleven thirty" is two times.
- The text a tool returns for the caller to confirm is the caller's own: the message of
  `send_message_to_doctor` and the reason of `take_message`. Where the reply reads it back in the same turn (compared without case and repeated
  spaces, whole or sentence by sentence), the advice and refill rules skip it; the SSN rules and
  the patient-data rule read the whole sentence. A paraphrased read-back may still be blocked.
- Sentences are split as the text comes (`split_sentences`), each character read once however
  the text is cut: a sentence is released only once the character after its end is known, so a
  blocked sentence is never partly shown. A full stop before a lower-case letter (`a.m. with`),
  after a title (`Dr.`, `Mr.`, `Mrs.`, `Ms.`) or after a single capital letter does not end a
  sentence; `10:30 AM. Is ...` does. Text with no end is flushed at the end of the reply. A
  reply is cut at the last complete sentence within 600 characters; a first sentence longer
  than that gives the fallback line.
- In the turn: what passed is said; a blocked sentence is replaced by its line and the rest of
  the reply is dropped. The blocked text never enters the conversation, a log line or the audit
  log. Each block writes `guard_block` (resource `call`, outcome `refused`, the rule's name as
  the reason) in a task of its own after the reply, so the reply never waits on the database;
  if it cannot be written the failure is logged (`guard_audit_failed`) and the block stands.
  At shutdown the app waits up to 10 seconds for those writes (`drain_guard_audits`, beside
  the call events' drain) and cancels any still running, logged as `guard_audit_cancelled`. Two blocked replies in a row raise the hand-off signal; a
  model reply said in full starts the count again. The page shows a blocked turn as the line
  said plus "(a reply was withheld: <rule>)".

Limits of the output guard:

- The gate, not this guard, is what keeps patient data from unverified callers: before
  verification the model has no patient data to say. The patient-data rule looks only for an
  amount, a lab status or a dosage; a sentence before verification without one of those
  passes, a medication's name included (it is what the caller said).
- The advice and refill rules are patterns: they cannot catch every phrasing of advice, a
  diagnosis or a promise ("Try ibuprofen" passes; so does a promise after an unrelated "if" or
  "when" earlier in the sentence), and they block some harmless sentences. Pinned in the tests
  as known false positives: "I can't tell you about the side effects of ...", "whether your
  results are ready" before verification, a paraphrased read-back of the caller's message,
  a seven-digit local number ending in the caller's last four, a year in numerals that equals
  the caller's last four ("December 3, 2026" for 2026) and a time in words that does ("ten
  thirty" for 1030).
- Missed by the digit reading: the last four said as a cardinal number ("three thousand three
  hundred forty-four"), or with homophones ("three three for four"); an SSN written with
  commas between its groups ("123, 45, 6789"), which is three runs; a dosage without a number
  next to its unit ("half a tablet").
- A verified caller's last four is only known when they gave it on this call; a sentence that
  says another patient's digits passes `ssn_last4` (the model never has them: tools act on the
  caller's own record only).
