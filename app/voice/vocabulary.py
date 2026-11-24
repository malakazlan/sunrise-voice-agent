"""Speech-to-text's vocabulary hints: the clinic's own words, which Deepgram's Nova-3 is told to
expect ("keyterms") so that a drug name is heard as the drug ("Lisinopril", not "listen oral").

The list is built once, at start-up: the distinct medication names of the medications table cut
to their drug word ("Lisinopril 10mg" is "Lisinopril"), the doctors' surnames and the clinic's
name. It is vocabulary only, with no link to any patient: a term is a word or a few of letters
only, 2 to 40 characters, so no dose, number, date or id can be one. Sorted, each once, at most
MAX_TERMS (Deepgram's own limit is 100), the doctors and the clinic always among them. It is
sent to Deepgram with every call.

The read goes through the runtime role's engine, bounded by its connect and statement timeouts
(app/db/session.py). When it fails the list is the doctors and the clinic, and one line is
logged.
"""

import logging
import re
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Medication
from app.domain.clinic import DOCTORS

logger = logging.getLogger(__name__)

CLINIC_NAME = "Sunrise Family Clinic"
MAX_TERMS = 100
# A drug word: letters only, 2 to 40 of them.
DRUG_WORD = re.compile(r"[A-Za-z]{2,40}")
# Always in the list: the doctors' surnames ("Dr. Patel" is "Patel") and the clinic.
FIXED_TERMS = frozenset({doctor.split()[-1] for doctor in DOCTORS} | {CLINIC_NAME})


def _drug_word(name: str) -> str | None:
    words = name.split()
    if words and DRUG_WORD.fullmatch(words[0]):
        return words[0]
    return None


def keyterms(medication_names: Iterable[str]) -> tuple[str, ...]:
    """The hints for these medication names: the fixed terms and as many drug words, in
    alphabetical order, as fit under MAX_TERMS; sorted."""
    drugs = {word for name in medication_names if (word := _drug_word(name)) is not None}
    room = MAX_TERMS - len(FIXED_TERMS)
    return tuple(sorted(FIXED_TERMS | set(sorted(drugs - FIXED_TERMS)[:room])))


class Vocabulary:
    """The hints a call's speech-to-text is given: the doctors and the clinic until `load` has
    read the medications table."""

    def __init__(self) -> None:
        self.terms = keyterms(())

    async def load(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        try:
            async with sessions() as session:
                names = (await session.scalars(select(Medication.name).distinct())).all()
        except Exception as error:
            logger.warning("stt_hints_fallback error=%s", type(error).__name__)
            return
        self.terms = keyterms(names)
        logger.info("stt_hints_loaded terms=%d", len(self.terms))
