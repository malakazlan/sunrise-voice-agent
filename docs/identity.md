# Identity verification and the input guard

Part of the [README](../README.md)'s reference.

Before any patient information, the caller gives a full name, a date of birth and the last
four digits of their SSN; all three must match one record, and the digits never reach the
model. Code decides; the model only relays the name and the date.

- `app/guard/input.py`: `guard_input(text, state)` decides each caller turn, purely (no
  database, network or clock), in this order. **Emergency**: a phrase of the emergency list
  (chest pain, can't breathe, suicid..., overdose, stroke, heart attack, passed out, severe
  bleeding and the others), negated or not, wins in every phase: the model sees nothing of
  the turn, code speaks the `emergency` line and the call moves to `emergency`. **The SSN
  step**, while armed: a turn with any digit or number word (numerals in any script, zero to
  nine, the teens, the tens, hundred, thousand, double, triple, and the homophones won, to,
  too, tree, for, fore, ate when they stand next to a digit or each other) is swallowed whole
  and its digits are collected; a turn without one passes. Number words are read where that
  is unambiguous: `nineteen thirty` is 1930, `thirty three forty four` is 3344, `sixteen
  hundred` is 1600. Four digits in all submit them; more than four clear the buffer and ask
  again (`ssn_too_long`); the third armed turn without four submits what there is.
  **Redaction**, always: digits after an SSN cue, SSN-shaped numbers, and any run of digits
  or number words that reads as exactly nine digits, whatever its grouping (spaces, hyphens,
  dots, the word "dash"). Before verification, in every phase but a new patient's `booking`,
  every other digit and number word is hidden too, except inside a date the mask module
  recognises, a year from 1900 to 2099 (numerals or words) or a run of ten or eleven digits (a
  callback number); in `booking` a new patient gives times and member numbers, and only SSNs
  are hidden. While a claim is pending (the step armed, or its digits awaiting the submit) and
  once verification is locked, the call cannot move to `booking`, and `start_new_patient` is
  not offered: the step cannot be left for the phase where numbers pass. Once
  verified, any group that reads as the caller's own last four is hidden; a phone number that
  ends in those digits is not. **Flags**: an attempt to override the instructions, and a
  request about someone else; the text still passes. The decision carries the state change as
  a transition; its caller applies it with `apply`.
- `app/guard/mask.py` stays the one owner of the SSN patterns: the guard uses its cue and SSN
  alternatives and its nine-digit rule (`mask_ssns`, which `mask()` for the logs and the
  doctor-message tool use too), its allow-list for unverified callers
  (`mask_unverified_digits`), its last-four rule (`mask_known_digits`) and its digit reading
  (`spoken_digits`, `says_a_number`). A repo scan refuses SSN patterns or lists of digit words
  anywhere else under `app/`; it is a lint, like the others.
- `verify_identity(full_name, dob)`: the model calls it in `intake` with the office open.
  The date must be an ISO date (`YYYY-MM-DD`) from 1900-01-01 to the clinic's date, and the
  name must
  be a name; otherwise the arguments are refused and nothing is counted. It stores the claim,
  arms the SSN step and returns `ssn_ask`. If the call is locked, or the claimed identity has
  five failures within the last hour across calls, it returns `verify_locked`, arms nothing
  and ends any claim the call already held. It never reads a patient record.
- `submit_identity()`: code-only; `submit_claim` in `app/agent/tools.py` calls it when the
  guard says submit. It reads the claim and the digits from the state, selects the records
  with exactly that date of birth and those four digits, and `pick_patient` must find exactly
  one by name. A match verifies the caller (`verified`, with their first name) and keeps the
  digits as `verified_ssn4`. Anything else is one failed attempt, identical whatever did not
  match: the same `verify_fail` line, the same `identity_refused` event, the same state
  change; the third locks the call (`verify_locked`, `identity_locked`). Every submit is
  recorded in `verification_attempts`, in one transaction with the state change and the
  audit event. Before anything else that transaction takes an advisory lock on the claim's
  key and counts its failures again: calls armed at the same moment submit one after another,
  and once the identity has five failures a submit returns `verify_locked` and counts
  nothing, so no more than five failures are recorded in the hour. A submit that does not run
  to an outcome (the database is down) counts nothing and verifies nothing, and
  `submit_claim` drops the claim and the digits.
- `verification_attempts` holds the claim's keyed hash (HMAC-SHA256 under
  `IDENTITY_HASH_KEY` over the name's letters and the ISO date), the call id, whether it
  matched and when; never a name, a date or a digit. The runtime role may only read and
  insert. A success does not clear earlier failures; failures of claims that match nobody
  count the same, so a lock says nothing about who exists.
- The claim and the SSN step live in `intake` with the office open; any phase change ends
  them, and a second claim replaces the first. `pending_ssn4` and `verified_ssn4` are excluded
  from every dump and from the state's text form by their declaration; validation errors do
  not show input. Events carry `identity_claimed`, `identity_verified` (the patient id),
  `identity_refused` and `identity_locked`, with no factor, name or digit. The scripted lines
  are in `app/agent/scripts.py`.

Limits, stated plainly:

- A last four that reads as a year (1900 to 2099), said before the step is armed and
  without a cue, reaches the model. The cost of the allow-list: before verification, the
  model does not see other small numbers either ("I have 2 questions", a time such as 10:30);
  only dates, years and callback numbers pass. In other phases before verification (a new
  patient's booking), numbers other than SSNs pass as said.
- Spoken number words count as digits while the step is armed: "one moment" adds a 1, and a
  turn with "double" or "a thousand" is swallowed though it adds nothing. "oh" is a zero only
  next to another digit; the homophones are digits only next to a digit or each other ("for
  to for to"), so "can I talk to a person" passes. The "four" of "last four" is not a digit.
- Anyone who knows a patient's name and date of birth can lock that patient out of phone
  verification for an hour with five failed calls; a transfer to staff remains open.
- On the third armed turn without digits the decision both submits and passes the text: the
  caller of the guard runs the submit first, then gives the model the text. A third turn with
  more than four digits submits an empty buffer.
- An emergency while armed: the model sees nothing of the turn; the move to `emergency` ends
  the step.
- `pydantic`'s `errors()` returns a rejected value when asked for its input; the error's text
  form does not. No application code reads `errors()` of a state.
