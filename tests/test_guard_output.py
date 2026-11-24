"""The output guard: each sentence of the model's own words, checked in code before it is said.
Every row is written by hand: the sentence, the state it is said in, and the rule that blocks it
(None: it may be said). SSN-like samples are built from pieces at run time, so that no file holds
one; the seed's phone numbers and last fours come from the seed file."""

import json
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime

from conftest import REPO_ROOT
from test_mask import ADVERSARIAL, DIGIT_ADVERSARIAL, LINEAR_BOUND_SECONDS, MEASURE_TIMEOUT_SECONDS
from test_repo_hygiene import forbidden_uses, imported_modules

from app.agent.scripts import SCRIPTS, ScriptId, placeholders
from app.agent.state import CallState, IdentityClaim, Phase, SsnStep, apply, move, new_call, verify
from app.agent.tool_registry import FAILED_MESSAGE, REFUSED_MESSAGE
from app.db.seed import load_seed_data
from app.guard.output import INSTEAD, GuardedReply, guard_output, guard_reply, split_sentences
from app.voice.calls import CallRegistry

T0 = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)

FIRST_PAIR, SECOND_PAIR = "33", "44"
LAST_FOUR = FIRST_PAIR + SECOND_PAIR
NINE_PARTS = ("123", "45", "6789")

# Each digit as a word, for saying a number the way a caller hears it.
WORDS = {
    "0": "zero",
    "1": "one",
    "2": "two",
    "3": "three",
    "4": "four",
    "5": "five",
    "6": "six",
    "7": "seven",
    "8": "eight",
    "9": "nine",
}


def new_call_id() -> str:
    call_id = CallRegistry(max_calls=1, max_seconds=1).reserve()
    assert call_id is not None
    return call_id


def intake(office_open: bool = True) -> CallState:
    state = new_call(call_id=new_call_id(), started_at=T0, office_open=office_open)
    return move(state, Phase.INTAKE)


def verified_as(patient_id: str, first_name: str, last_four: str) -> CallState:
    """A caller verified, in office hours, with the four digits they gave."""
    claimed = apply(intake(), IdentityClaim(full_name="Test Caller", dob=date(1985, 3, 14)))
    collected = apply(claimed, SsnStep(expecting_ssn4=False, ssn_turns=0, pending_ssn4=last_four))
    return verify(collected, patient_id=patient_id, first_name=first_name)


SARAH = verified_as("P001", "Sarah", LAST_FOUR)
UNVERIFIED = intake()
CLOSED = intake(office_open=False)
# The state model refuses a verified caller while the office is closed; code that forges one
# (model_copy does not validate) still gets the closed-office answer.
FORGED_CLOSED = SARAH.model_copy(update={"office_open": False})


def rule(sentence: str, state: CallState) -> str | None:
    decision = guard_output(sentence, state)
    return None if decision is None else decision.rule.value


# (sentence, the rule that blocks it or None)
Rows = list[tuple[str, str | None]]


def mismatches(rows: Rows, state: CallState) -> list[tuple[str, str | None, str | None]]:
    """Each row whose rule is not the one written, with the rule it got."""
    return [
        (sentence, expected, rule(sentence, state))
        for sentence, expected in rows
        if rule(sentence, state) != expected
    ]


def test_the_line_said_instead() -> None:
    assert {name.value: script.value for name, script in INSTEAD.items()} == {
        "ssn_last4": "fallback",
        "ssn_shape": "fallback",
        "phi_outside_gate": "fallback",
        "medical_advice": "advice_refusal",
        "refill_promise": "refill_no_promise",
    }
    # The rules in the order they are checked.
    assert [name.value for name in INSTEAD] == [
        "ssn_last4",
        "ssn_shape",
        "phi_outside_gate",
        "medical_advice",
        "refill_promise",
    ]
    decision = guard_output("You should skip your next dose.", SARAH)
    assert decision is not None
    assert (decision.rule.value, decision.script) == ("medical_advice", ScriptId.ADVICE_REFUSAL)


# --- R1 -------------------------------------------------------------------------------------


def test_verified_last_four_is_blocked() -> None:
    assert load_seed_data()["patients"][0]["ssn_last4"] == LAST_FOUR
    said = [
        f"I have {LAST_FOUR} on file.",
        f"The last four I have are {LAST_FOUR}.",
        "I have three three four four on file.",
        f"I have {' '.join(LAST_FOUR)} on file.",
        f"I have {'-'.join(LAST_FOUR)} on file.",
        f"I have {FIRST_PAIR} {SECOND_PAIR} on file.",
        f"I have {FIRST_PAIR}-{SECOND_PAIR} on file.",
        "I have three, three, four, four on file.",
        "I have double three double four on file.",
        "I have thirty-three forty-four on file.",
        "I have thirty three forty four on file.",
        "I have three three forty four on file.",
        # Digits of another script.
        "I have ３３４４ on file.",
        # In the middle of a sentence, and at its end.
        f"Thanks, so {LAST_FOUR} matches and you're all set.",
        f"Got it, {LAST_FOUR}.",
        # Groups joined across commas, dashes of any length, "and", and um or uh.
        "I have thirty-three, forty-four on file.",
        f"I have {FIRST_PAIR}, {SECOND_PAIR} on file.",
        f"I have {FIRST_PAIR} – {SECOND_PAIR} on file.",
        f"I have {FIRST_PAIR}—{SECOND_PAIR} on file.",
        f"I have {FIRST_PAIR} and {SECOND_PAIR} on file.",
        f"I have {FIRST_PAIR}, um, {SECOND_PAIR} on file.",
        "I have thirty-three uh forty-four on file.",
    ]
    assert mismatches([(sentence, "ssn_last4") for sentence in said], SARAH) == []

    # The reply so far: two sentences that each say half are blocked at the second.
    split = guard_reply([f"Your last four are {FIRST_PAIR}. {SECOND_PAIR}. Anything else?"], SARAH)
    assert split.said == (f"Your last four are {FIRST_PAIR}.",)
    assert split.block is not None
    assert split.block.rule.value == "ssn_last4"
    # A limit, pinned: the cardinal reading is not read as the four digits.
    assert rule("I have three thousand three hundred forty-four on file.", SARAH) is None
    # Said next to other numbers it is still said; inside a longer number it is not.
    assert rule(f"It's {LAST_FOUR} and 10 more.", SARAH) == "ssn_last4"
    assert rule(f"The order number is {'1' + LAST_FOUR + '5'}.", SARAH) is None

    # Another patient's own digits, in words.
    michael = verified_as("P002", "Michael", "10" + "20")
    assert rule("I have ten twenty on file.", michael) == "ssn_last4"
    assert rule("I have one zero two zero on file.", michael) == "ssn_last4"

    # Only the caller's own: the same digits for another caller, or before verification, pass.
    assert rule(f"I have {LAST_FOUR} on file.", michael) is None
    assert rule(f"I have {LAST_FOUR} on file.", UNVERIFIED) is None

    decision = guard_output(f"I have {LAST_FOUR} on file.", SARAH)
    assert decision is not None
    assert decision.script is ScriptId.FALLBACK


# --- R2 -------------------------------------------------------------------------------------


def phone_forms(phone: str) -> list[str]:
    """The seed's number (NNN-NNN-NNNN) as a model may read it back."""
    area, exchange, line = phone.split("-")
    words = ", ".join(" ".join(WORDS[digit] for digit in part) for part in (area, exchange, line))
    return [
        phone,
        phone.replace("-", " "),
        phone.replace("-", "."),
        phone.replace("-", ""),
        f"({area}) {exchange}-{line}",
        f"1-{phone}",
        f"+1 {area} {exchange} {line}",
        f"{area}, {exchange}, {line}",
        words,
    ]


def test_phone_readback_is_allowed() -> None:
    patients = load_seed_data()["patients"]
    assert len(patients) == 5
    for patient in patients:
        state = verified_as(patient["id"], patient["first_name"], patient["ssn_last4"])
        # Every seed number ends in the patient's own last four.
        assert patient["phone"].endswith(patient["ssn_last4"])
        rows: list[tuple[str, str | None]] = [
            (f"I have your callback number as {form}.", None)
            for form in phone_forms(patient["phone"])
        ]
        rows += [
            ("Your email is s a r a h dot j at example dot com.", None),
            ("I'll call you back at that number.", None),
            # A known false positive, pinned: seven digits are no callback number, so the last
            # four in them are said.
            (f"I have your number as {patient['phone'][4:]}.", "ssn_last4"),
            # Digits joined by "and" are no phone number.
            (f"I have 555 201 and {patient['ssn_last4']}.", "ssn_last4"),
        ]
        assert mismatches(rows, state) == [], patient["id"]


# --- R3 -------------------------------------------------------------------------------------


def nine_digit_samples() -> list[str]:
    return [
        "-".join(NINE_PARTS),
        " ".join(NINE_PARTS),
        ".".join(NINE_PARTS),
        "".join(NINE_PARTS),
        " - ".join(NINE_PARTS),
        " ".join("".join(NINE_PARTS)),
        " ".join(["123", "456", "789"]),
        " ".join(["12345", "6789"]),
        "one two three four five six seven eight nine",
        "one two three dash four five dash six seven eight nine",
        "one, two, three, four, five, six, seven, eight, nine",
        # Inside letters: no exception for an id.
        "P" + "".join(NINE_PARTS),
        # Digits of another script.
        "-".join(["１２３", "４５", "６７８９"]),
    ]


def test_ssn_shapes_are_blocked() -> None:
    for state in (SARAH, UNVERIFIED, CLOSED):
        rows: list[tuple[str, str | None]] = [
            (f"The number on file is {sample}.", "ssn_shape") for sample in nine_digit_samples()
        ]
        rows += [
            # Eight, ten and eleven digits are not nine.
            (f"The order number is {' '.join(['12', '345', '678'])}.", None),
            (f"The order number is {' '.join(['1234', '5678'])}.", None),
            (f"Call us at {'-'.join(['555', '201', '9000'])}.", None),
            (f"Call us at {'-'.join(['1', '555', '201', '9000'])}.", None),
            # A known miss, pinned: a comma joins a run only between single digits, so
            # comma-separated groups are three runs.
            (f"The number on file is {', '.join(NINE_PARTS)}.", None),
            # "to", "for", "won" and "ate" are words here, not digits.
            ("We have an opening from ten thirty to eleven thirty.", None),
        ]
        assert mismatches(rows, state) == [], state.verified

    # Order: the caller's last four is checked before the nine-digit shape.
    both = f"{LAST_FOUR} and {'-'.join(NINE_PARTS)}"
    assert rule(both, SARAH) == "ssn_last4"
    assert rule(both, UNVERIFIED) == "ssn_shape"


# --- R4 -------------------------------------------------------------------------------------

# Patient data: blocked before verification or while the office is closed, allowed for a
# verified caller in office hours.
PHI = [
    # A money amount.
    "Your balance is $45.00.",
    "You owe forty-five dollars.",
    "Your balance is 120 dollars and 50 cents.",
    "You have a balance of $15.25.",
    "Your balance is zero.",
    "That comes to $45.00.",
    "It's forty-five dollars.",
    # A lab status.
    "Your lab results are ready, and the doctor will call to discuss them.",
    "Your results are still pending.",
    "You have no recent labs on file.",
    "Your test results came back.",
    "The doctor will call you to discuss your results.",
    # A dosage.
    "You take Lisinopril 10mg.",
    "Your Metformin is 500 mg.",
    "It comes in 50 mcg.",
    "It comes as 5 ml.",
    "It is twenty milligrams.",
    "That's two tablets a day.",
    "That's 10 units at night.",
]

# The same kinds of words without patient data: allowed in every state.
NOT_PHI = [
    "I can help with a refill once I verify your identity.",
    "I can check your balance once you're verified.",
    "What is the name of the medication?",
    "I can look up your lab results after I verify your identity.",
    "Our hours are Monday to Friday, 9 AM to 5 PM.",
    "Which pharmacy should we send the refill to?",
    # A date of birth said back to the caller who said it.
    "I have your date of birth as March 14, 1985.",
    "Please spell your last name for me.",
    "Thank you, Sarah.",
    "Is this a refill for a medication you take every day?",
    "Dr. Lee can review your refill request on Monday once you're verified.",
    # Names, pharmacies and drug names next to refill, prescription or medication: before
    # verification it is what the caller said (the gate keeps the record from the model).
    "Okay, Michael, I can help with a refill once I verify your identity.",
    "Sure, Sarah, I can help with your refill once you're verified.",
    "I can send your refill to CVS Main St once I verify your identity.",
    "Thanks, Maria, I'll need to verify you before I look at your prescription.",
    "I can help with your Lisinopril refill once I verify your identity.",
    "I can request a refill of Atorvastatin once you're verified.",
    "Do you need a refill of Zoloft?",
    "Is the refill for lisinopril?",
]

# Known false positives, pinned: harmless sentences the pattern rules block outside the gate.
PHI_FALSE_POSITIVES = [
    # A status word next to "results" reads as a lab status.
    "Once you're verified, I can check whether your results are ready.",
]

# Known misses, pinned: patient data the patterns do not see.
PHI_MISSES = [
    # A dosage without a number next to its unit.
    "You take half a tablet.",
]


def test_phi_outside_the_gate() -> None:
    for state in (UNVERIFIED, CLOSED, FORGED_CLOSED):
        rows: Rows = [(s, "phi_outside_gate") for s in PHI + PHI_FALSE_POSITIVES]
        rows += [(s, None) for s in NOT_PHI + PHI_MISSES]
        assert mismatches(rows, state) == [], (state.verified, state.office_open)
    # Both ways: the same sentences are allowed for a verified caller in office hours.
    allowed: Rows = [(s, None) for s in PHI + PHI_FALSE_POSITIVES + NOT_PHI]
    assert mismatches(allowed, SARAH) == []

    # Order: patient data outside the gate is checked before advice.
    both = "Your balance is $45.00, and you should take ibuprofen for that."
    assert rule(both, UNVERIFIED) == "phi_outside_gate"
    assert rule(both, SARAH) == "medical_advice"
    # And after the nine-digit shape.
    shaped = f"Your balance is $45.00 and the number is {'-'.join(NINE_PARTS)}."
    assert rule(shaped, UNVERIFIED) == "ssn_shape"


# --- R5 -------------------------------------------------------------------------------------

ADVICE = [
    "You should take two tablets of ibuprofen.",
    "You shouldn't take ibuprofen with that.",
    "You should not take it on an empty stomach.",
    "You should stop taking your Lisinopril.",
    "You should start taking it in the morning.",
    "You should increase your dose.",
    "You should decrease your dose.",
    "You should double your dose tonight.",
    "You should skip your next dose.",
    "You need to take your medication with food.",
    "You should probably stop your Metformin.",
    "Stop taking it right away.",
    "Don't take more than two a day.",
    "It's probably just a cold.",
    "It’s probably nothing serious.",
    "It is probably a sinus infection.",
    "That's probably a migraine.",
    "It sounds like you have the flu.",
    "It sounds like you might have an allergy.",
    "You may have an infection.",
    "You might have a sinus infection.",
    "You probably have a cold.",
    "That's normal after starting a new medication.",
    "It's completely normal to feel tired.",
    "That's nothing to worry about.",
    "There's nothing to worry about.",
    "I recommend taking it with food.",
    "I'd suggest trying an antihistamine.",
    "I would recommend trying a warm compress.",
    "It's safe to take ibuprofen with that.",
    "Dizziness is a common side effect of Lisinopril.",
    "Nausea is one of the side effects of Metformin.",
    "I'd recommend you take it with food.",
    "You can take ibuprofen for that.",
    "It could be a sinus infection.",
    "That sounds like a migraine.",
    "You likely have a cold.",
    "It's fine to take them together.",
    "It's okay to take it at night.",
    "It's safe to take two.",
    "It's okay to skip a dose.",
    "Hold off on taking it until you talk to your doctor.",
]

NOT_ADVICE = [
    "You should receive a confirmation.",
    "It's probably best to speak with our staff.",
    "It's probably easiest to call back tomorrow.",
    "You should hear back within two business days.",
    "You should get a call from our nurse.",
    "You should start by telling me your full name.",
    "You should stop by the front desk when you arrive.",
    "I'd recommend speaking with your doctor about that.",
    "I recommend calling back during office hours.",
    "I'd suggest trying to call back after 9 AM.",
    "You may have to wait a few minutes.",
    "You might have received a reminder text.",
    "You may have already received a confirmation.",
    "That's normally handled by our billing team.",
    "Our doctors can talk with you about side effects.",
    "It sounds like you'd like to book an appointment.",
    "If this is an emergency, you should hang up and call 911.",
    "I'm not able to give medical advice.",
    "You may have a copay at your visit.",
    "You might have a message from our office.",
    "I'd suggest trying our patient portal.",
    "It's normal for lab results to take a few days.",
    "It's normal for test results to take a week.",
    "It's probably in your spam folder.",
    "You should take your insurance card to the visit.",
    "You should take a moment to find your insurance card.",
    "You can take your time.",
    "It could be a few days before we hear back.",
    "That sounds like a good plan.",
    "You probably have your insurance card handy.",
]

# Known false positives, pinned: ordinary sentences an advice phrase blocks.
ADVICE_FALSE_POSITIVES = [
    "I can't tell you about the side effects of that medication.",
]

# Known misses, pinned: advice in words the phrases do not cover.
ADVICE_MISSES = ["Try ibuprofen.", "Rest and drink plenty of fluids."]


def test_medical_advice() -> None:
    rows: Rows = [(s, "medical_advice") for s in ADVICE + ADVICE_FALSE_POSITIVES]
    rows += [(s, None) for s in NOT_ADVICE + ADVICE_MISSES]
    assert mismatches(rows, SARAH) == []
    # Advice is advice before verification too.
    unverified: Rows = [
        (s, "medical_advice") for s in ["It's probably just a cold.", "You should skip your dose."]
    ]
    assert mismatches(unverified, UNVERIFIED) == []

    # Order: advice is checked before a refill promise.
    assert rule("Your refill is approved, so you should take two a day.", SARAH) == (
        "medical_advice"
    )


# --- R6 -------------------------------------------------------------------------------------

PROMISES = [
    "Your refill has been approved.",
    "Your refill is approved.",
    "Your refill will be approved today.",
    "Your Lisinopril refill was approved.",
    "Your prescription is ready for pickup.",
    "Your prescription is now ready.",
    "Your prescription's ready.",
    "Your prescriptions are ready.",
    "The pharmacy says your refill is ready.",
    "Your prescription has been filled.",
    "Your prescriptions have been filled.",
    "Your refill will be ready tomorrow.",
    "Your medication is all set.",
    "Your Lisinopril has been refilled.",
    "It's been approved, so your prescription is on its way.",
    # A hedge after the promise does not take it back.
    "Your refill is approved, so call us if you have questions.",
]

NOT_PROMISES = [
    "Your refill request has been sent.",
    "I've sent your refill request to CVS Main St.",
    "Your refill request has been sent to your doctor for review.",
    "Your doctor will review the refill request.",
    "Your lab results are ready.",
    "I'll let the pharmacy know about your prescription.",
    # Not a promise: the word is asked about, waited for or denied.
    "I can't see whether your refill has been approved.",
    "Your doctor will decide whether the refill is approved.",
    "I don't know if your prescription is ready.",
    "Once your refill is approved, the pharmacy will text you.",
    "Refills must be approved by your doctor.",
    "Your refill request is ready to send; shall I submit it?",
    "Your prescription is not approved yet.",
    "When your prescription is ready, the pharmacy will call you.",
]


def test_refill_promise() -> None:
    rows: Rows = [(s, "refill_promise") for s in PROMISES]
    rows += [(s, None) for s in NOT_PROMISES]
    assert mismatches(rows, SARAH) == []
    assert rule(PROMISES[0], UNVERIFIED) == "refill_promise"
    decision = guard_output(PROMISES[0], SARAH)
    assert decision is not None
    assert decision.script is ScriptId.REFILL_NO_PROMISE
    # The review's reply: an honest denial, said whole.
    denial = (
        "I can't see whether your refill has been approved. I can send a request to your"
        " doctor if you'd like."
    )
    assert guard_reply([denial], SARAH) == GuardedReply(
        said=(
            "I can't see whether your refill has been approved.",
            "I can send a request to your doctor if you'd like.",
        ),
        block=None,
    )


# --- R7 -------------------------------------------------------------------------------------

# What the model says with each seed patient's tool results, as the patient's own verified
# caller: (patient id, reply, its sentences).
ANSWERS = [
    (
        "P001",
        "Your next appointment is Thursday, December 3 at 10:30 AM with Dr. Patel. Is there"
        " anything else I can help with?",
        (
            "Your next appointment is Thursday, December 3 at 10:30 AM with Dr. Patel.",
            "Is there anything else I can help with?",
        ),
    ),
    ("P001", "Your balance is $45.00.", None),
    ("P001", "You have a balance of forty-five dollars.", None),
    ("P001", "Your lab results are ready, and the doctor will call to discuss them.", None),
    ("P001", "I've requested a refill of Lisinopril 10mg at CVS Main St.", None),
    ("P001", "Just to confirm, you'd like a refill of Lisinopril 10mg sent to CVS Main St?", None),
    (
        "P001",
        "Metformin 500mg has no refills left, so I've sent the request to your doctor.",
        None,
    ),
    ("P001", "Your refill request has been sent.", None),
    ("P001", "I couldn't find that medication on your list.", None),
    ("P001", "Your message has been sent to your doctor.", None),
    ("P002", "You have no upcoming appointments.", None),
    ("P002", "Your balance is $0.00.", None),
    ("P002", "Your lab results are still pending.", None),
    ("P002", "I've requested a refill of Atorvastatin 20mg at Walgreens Oak Ave.", None),
    ("P003", "Your next appointment is Saturday, November 28 at 2:00 PM with Dr. Lee.", None),
    ("P003", "Your balance is $120.50.", None),
    ("P003", "You have no recent labs on file.", None),
    ("P003", "I've requested a refill of Levothyroxine 50mcg at Rite Aid 5th St.", None),
    ("P004", "Your lab results are still pending.", None),
    ("P004", "You don't have any medications on file.", None),
    ("P005", "Your balance is $15.25.", None),
    (
        "P005",
        "Albuterol inhaler has no refills left, so I've sent the request to your doctor.",
        None,
    ),
]


# A first-time caller booking: never verified.
NEW_PATIENT = move(intake(), Phase.BOOKING)

# What the model says with booking's tool results (app/agent/booking.py), written by hand.
BOOKING_ANSWERS = [
    "I have Dr. Patel on Wednesday, November 25 at 9:30 AM, or Dr. Lee on Thursday,"
    " November 26 at 10:00 AM.",
    "Dr. Patel on Wednesday, November 25 at 9:30 AM. Shall I book it?",
    "You're booked with Dr. Patel on Wednesday, November 25 at 9:30 AM.",
    "I have your email as A N N dot E X at example dot com.",
    "I have your phone number as 555-410-2233.",
    "That time was just taken. Would you like another one?",
    # The endings tools' (app/agent/endings.py): a callback message read back, a transfer.
    "I'll leave a message for Ana Diaz to be called back at 555-410-2233 about a billing"
    " question. Is that right?",
    "Your message has been recorded.",
    "Would you like me to transfer you to our staff?",
]


def seed_states() -> dict[str, CallState]:
    return {
        patient["id"]: verified_as(patient["id"], patient["first_name"], patient["ssn_last4"])
        for patient in load_seed_data()["patients"]
    }


def every_line_said_to_the_model(first_name: str) -> list[str]:
    """Every scripted line, with its values as code fills them, and the tools' fixed texts."""
    values = {"first_name": first_name, "name": "Ana Diaz", "part_of_day": "night"}
    lines = [
        SCRIPTS[script].text.format(**{key: values[key] for key in placeholders(script)})
        for script in ScriptId
    ]
    assert len(lines) == 15
    return [*lines, REFUSED_MESSAGE, FAILED_MESSAGE]


def test_real_answers_pass() -> None:
    states = seed_states()
    for patient_id, reply, sentences in ANSWERS:
        guarded = guard_reply([reply], states[patient_id])
        assert (guarded.said, guarded.block) == (sentences or (reply,), None), reply

    for state in [*states.values(), UNVERIFIED, CLOSED, NEW_PATIENT]:
        first_name = state.patient_first_name or "Sarah"
        for line in every_line_said_to_the_model(first_name):
            guarded = guard_reply([line], state)
            assert guarded.block is None, (line, guarded.block)
            assert " ".join(guarded.said) == line

    # Booking's answers, for a verified caller and for a first-time caller.
    for state in (states["P001"], NEW_PATIENT):
        for reply in BOOKING_ANSWERS:
            guarded = guard_reply([reply], state)
            assert (" ".join(guarded.said), guarded.block) == (reply, None), reply

    # A year in numerals, or a time in words, that reads as the caller's own last four is
    # blocked: a known false positive, pinned. A time in numerals with a colon is two runs and
    # passes.
    year_caller = verified_as("P001", "Sarah", "20" + "26")
    time_caller = verified_as("P001", "Sarah", "10" + "30")
    assert rule(
        "Your next appointment is Thursday, December 3, 2026 at 10:30 AM with Dr. Patel.",
        year_caller,
    ) == ("ssn_last4")
    assert rule("Your appointment is at ten thirty AM with Dr. Patel.", time_caller) == (
        "ssn_last4"
    )
    assert rule("Your appointment is at 10:30 AM with Dr. Patel.", time_caller) is None


# The caller's own message to the doctor, as send_message_to_doctor returns it to be read back.
QUESTION = "Should I stop taking my Lisinopril because of the headaches?"
READ_BACK = f"Your message to Dr. Patel reads: {QUESTION} Shall I send it?"


def test_a_message_read_back_passes() -> None:
    # The message the tool returned is not the model's advice; the rest of the reply is.
    assert guard_reply([READ_BACK], SARAH, read_back=(QUESTION,)) == GuardedReply(
        said=(f"Your message to Dr. Patel reads: {QUESTION}", "Shall I send it?"), block=None
    )
    # Compared ignoring case and repeated spaces, with the message in quotes, or as two
    # sentences of a longer message.
    loose = f'Your message reads: "{QUESTION.lower().replace(" ", "  ")}" Shall I send it?'
    assert guard_reply([loose], SARAH, read_back=(QUESTION,)).block is None
    two = "I have headaches. Should I stop taking it?"
    assert (
        guard_reply([f"Your message reads: {two} Shall I send it?"], SARAH, read_back=(two,)).block
        is None
    )

    # Without the read-back, or outside it, the advice rule applies.
    plain = guard_reply([READ_BACK], SARAH)
    assert plain.block is not None
    assert plain.block.rule.value == "medical_advice"
    outside = guard_reply([f"{READ_BACK} You should stop taking it."], SARAH, read_back=(QUESTION,))
    assert outside.block is not None
    assert outside.block.rule.value == "medical_advice"
    # A paraphrased read-back may still be blocked: a known false positive, pinned.
    paraphrase = "Your message asks whether you should stop taking your Lisinopril."
    assert guard_output(paraphrase, SARAH, read_back=(QUESTION,)) is not None
    # A refill promise inside the read-back passes too.
    promise = "Is my refill approved?"
    assert guard_output(f"Your message reads: {promise}", SARAH, read_back=(promise,)) is None

    # The SSN rules and the outside-the-gate rule still see the whole sentence.
    with_digits = f"Please call me about {LAST_FOUR}."
    said = guard_output(f"Your message reads: {with_digits}", SARAH, read_back=(with_digits,))
    assert said is not None
    assert said.rule.value == "ssn_last4"
    nine = f"My number is {'-'.join(NINE_PARTS)}."
    shaped = guard_output(f"Your message reads: {nine}", SARAH, read_back=(nine,))
    assert shaped is not None
    assert shaped.rule.value == "ssn_shape"
    owed = "Do I still owe $45.00?"
    gated = guard_output(f"Your message reads: {owed}", UNVERIFIED, read_back=(owed,))
    assert gated is not None
    assert gated.rule.value == "phi_outside_gate"


# --- R8 -------------------------------------------------------------------------------------


def released(pieces: list[str]) -> list[tuple[str, int]]:
    """Each sentence, with how many pieces had been read when it came out."""
    read = 0

    def stream() -> Iterator[str]:
        nonlocal read
        for piece in pieces:
            read += 1
            yield piece

    return [(sentence, read) for sentence in split_sentences(stream())]


def test_sentences_and_blocking() -> None:
    # A sentence comes out only once the text after its end shows it is the end.
    assert released(
        ["Your next appointment is Thursday, December 3 at 10:30 AM with Dr", ". Pat", "el.", " Is"]
        + [" there anything else?"]
    ) == [
        ("Your next appointment is Thursday, December 3 at 10:30 AM with Dr. Patel.", 4),
        ("Is there anything else?", 5),
    ]
    assert released(["One moment.", " ", "Let me check."]) == [
        ("One moment.", 3),
        ("Let me check.", 3),
    ]

    rows = [
        (
            "Your appointment is at 10:30 a.m. with Dr. Patel. Anything else?",
            ["Your appointment is at 10:30 a.m. with Dr. Patel.", "Anything else?"],
        ),
        (
            "It is at 10:30 AM. Is there anything else?",
            ["It is at 10:30 AM.", "Is there anything else?"],
        ),
        ("Your balance is $45.00 today. Thanks!", ["Your balance is $45.00 today.", "Thanks!"]),
        ("Mrs. Johnson, Mr. Brown and Ms. Lee.", ["Mrs. Johnson, Mr. Brown and Ms. Lee."]),
        ("Really?! Yes.", ["Really?!", "Yes."]),
        ("First line.\nSecond line.", ["First line.", "Second line."]),
        # No end: flushed at the end of the reply.
        ("Your balance is forty-five dollars", ["Your balance is forty-five dollars"]),
        ("Sure. And then", ["Sure.", "And then"]),
        ("", []),
        ("   \n ", []),
    ]
    assert [(text, list(split_sentences([text]))) for text, _ in rows] == rows
    # Streamed a character at a time, the sentences are the same.
    assert [(text, list(split_sentences(iter(text)))) for text, _ in rows] == rows

    # A blocked sentence: what came before it is said, it is replaced, the rest is dropped.
    guarded = guard_reply(["Sure. You should skip ", "your next dose. Then call us back."], SARAH)
    assert guarded.said == ("Sure.",)
    assert guarded.block is not None
    assert (guarded.block.rule.value, guarded.block.script) == (
        "medical_advice",
        ScriptId.ADVICE_REFUSAL,
    )
    first = guard_reply([f"{LAST_FOUR} is on file. Anything else?"], SARAH)
    assert first.said == ()
    assert first.block is not None
    assert first.block.rule.value == "ssn_last4"

    # A reply over 600 characters is cut at the last complete sentence within the bound.
    hundred = "A" * 99 + "."
    ninety_five = "B" * 94 + "."
    long_reply = " ".join([hundred] * 5 + [ninety_five, "Last one."])
    assert guard_reply([long_reply], SARAH).said == (hundred,) * 5 + (ninety_five,)
    assert len(" ".join(guard_reply([long_reply], SARAH).said)) == 600
    # One sentence over the bound: nothing is said.
    over = guard_reply(["C" * 600 + "."], SARAH)
    assert (over.said, over.block) == ((), None)


# --- R11 ------------------------------------------------------------------------------------

OUTPUT_GUARD = REPO_ROOT / "app" / "guard" / "output.py"
OUTPUT_IMPORTS = {
    "re",
    "collections.abc",
    "dataclasses",
    "enum",
    "types",
    "app.agent.scripts",
    "app.agent.state",
    "app.guard.mask",
}

MEASURE = """
import json, sys, time
from datetime import UTC, date, datetime
from app.agent.state import IdentityClaim, Phase, SsnStep, apply, move, new_call, verify
from app.guard.output import guard_output, guard_reply, split_sentences
start = move(
    new_call(
        call_id="0f1e2d3c4b5a46978a1b2c3d4e5f6a7b",
        started_at=datetime(2026, 11, 24, 15, 0, tzinfo=UTC),
        office_open=True,
    ),
    Phase.INTAKE,
)
claimed = apply(start, IdentityClaim(full_name="Test Caller", dob=date(1985, 3, 14)))
collected = apply(
    claimed, SsnStep(expecting_ssn4=False, ssn_turns=0, pending_ssn4="33" + "44")
)
verified = verify(collected, patient_id="P001", first_name="Sarah")
readers = [
    lambda text: guard_output(text, verified),
    lambda text: guard_output(text, start),
    lambda text: list(split_sentences([text])),
    lambda text: guard_reply([text], start),
    # Streamed, a character at a time.
    lambda text: list(split_sentences(iter(text))),
    lambda text: guard_reply(iter(text), verified),
]
timings = []
for text in json.load(sys.stdin):
    for reader in readers:
        best = float("inf")
        for _ in range(3):
            started = time.perf_counter()
            reader(text)
            best = min(best, time.perf_counter() - started)
        timings.append(best)
print(json.dumps(timings))
"""

# Text that every rule's patterns would meet, many times over, in 10,000 characters.
GUARD_ADVERSARIAL = [
    "you should " * 909,
    "it's probably " * 714,
    "you may have " * 769,
    "refill approved " * 625,
    "Refill Zoloft " * 714,
    "10 mg " * 1_666,
    "mg " * 3_333,
    "lab results " * 833,
    "$" * 10_000,
    "Dr. " * 2_500,
    ". " * 5_000,
    "a.m. " * 2_000,
    "?" * 10_000,
]


def test_guard_is_pure_and_linear() -> None:
    # Pure: it imports nothing that reaches a database, the network, the clock or a file.
    source = OUTPUT_GUARD.read_text(encoding="utf-8")
    assert set(imported_modules(source, "app.guard")) <= OUTPUT_IMPORTS
    assert forbidden_uses(source) == []
    # The same sentence and state give the same decision, and the state is not touched.
    before = SARAH.model_copy()
    for sentence in [*ADVICE[:3], *PHI[:3], f"I have {LAST_FOUR} on file.", "Hello."]:
        assert guard_output(sentence, SARAH) == guard_output(sentence, SARAH)
    assert SARAH == before

    texts = ADVERSARIAL + DIGIT_ADVERSARIAL + GUARD_ADVERSARIAL
    assert max(len(text) for text in texts) <= 10_000
    try:
        measured = subprocess.run(
            [sys.executable, "-c", MEASURE],
            input=json.dumps(texts),
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=MEASURE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(f"the guard took over {MEASURE_TIMEOUT_SECONDS} s") from None
    timings = json.loads(measured.stdout)
    assert len(timings) == 6 * len(texts)
    assert max(timings) < LINEAR_BOUND_SECONDS, max(timings)
