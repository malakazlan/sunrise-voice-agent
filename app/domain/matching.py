"""Which patient record a claimed name belongs to, and which listed medication was meant.

The only owner of name and medication normalization. Nothing here looks anything up: the
caller passes the candidates, already matched on date of birth and SSN last four, or the
caller's own medication list.
"""

import re
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Protocol

import jellyfish

APOSTROPHES = str.maketrans("", "", "'’")
LATIN_LETTERS = re.compile(r"[a-z]+")

# A name part may differ by this many edits (Damerau-Levenshtein) when it sounds the same:
# "Braun" for "Brown" is two, "Michelle" for "Michael" is three.
MAX_NAME_EDITS = 2

# A medication name may differ by this many edits: "lisinipril" for "lisinopril" is one,
# "escitalopram" for "citalopram" (a different drug) is two.
MAX_MEDICATION_EDITS = 1

STRENGTH_AND_FORM = re.compile(
    r"\b(?:\d+(?:[.,]\d+)?\s*)?"
    r"(?:mg|mcg|g|ml|milligrams?|micrograms?|tablets?|capsules?|pills?)\b"
    r"|\b\d+(?:[.,]\d+)?\b"
)
NOT_LATIN_OR_DIGIT = re.compile(r"[^a-z0-9]")


class NamedRecord(Protocol):
    @property
    def first_name(self) -> str: ...

    @property
    def last_name(self) -> str: ...


def _fold(text: str) -> str | None:
    """Lower-cased, accents removed, apostrophes dropped; None if a non-Latin letter is left."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    folded = "".join(char for char in decomposed if not unicodedata.combining(char))
    folded = folded.translate(APOSTROPHES)
    if any(char.isalpha() and not ("a" <= char <= "z") for char in folded):
        return None
    return folded


def normalize_name(full_name: str) -> tuple[str, str] | None:
    """(first, last): the first and last token. Middle names are ignored.

    Punctuation separates tokens ("Sarah-Johnson." is two), apostrophes join them ("O'Brien"
    is one). Fewer than two tokens, or a letter outside the Latin alphabet, is not a name.
    """
    folded = _fold(full_name)
    if folded is None:
        return None
    tokens = LATIN_LETTERS.findall(folded)
    if len(tokens) < 2:
        return None
    return tokens[0], tokens[-1]


def name_key(full_name: str) -> str | None:
    """A phonetic key for the (first, last) pair: "Sara Johnson" and "Sarah Johnson" share one."""
    name = normalize_name(full_name)
    if name is None:
        return None
    first, last = name
    return f"{jellyfish.metaphone(first)} {jellyfish.metaphone(last)}"


def _sounds_like(spoken: str, recorded: str) -> bool:
    code = jellyfish.metaphone(spoken)
    return (
        code != ""
        and code == jellyfish.metaphone(recorded)
        and jellyfish.damerau_levenshtein_distance(spoken, recorded) <= MAX_NAME_EDITS
    )


def pick_patient[R: NamedRecord](claimed_full_name: str, candidates: Iterable[R]) -> R | None:
    """The one candidate the claimed name belongs to, or None.

    Exactly one strict match (normalized first and last equal) wins. Otherwise exactly one
    phonetic match wins. More than one match at the level being considered is None.
    """
    claimed = normalize_name(claimed_full_name)
    if claimed is None:
        return None
    named = [
        (candidate, normalize_name(f"{candidate.first_name} {candidate.last_name}"))
        for candidate in candidates
    ]

    strict = [candidate for candidate, name in named if name == claimed]
    if len(strict) == 1:
        return strict[0]

    phonetic = [
        candidate
        for candidate, name in named
        if name is not None
        and _sounds_like(claimed[0], name[0])
        and _sounds_like(claimed[1], name[1])
    ]
    if len(phonetic) == 1:
        return phonetic[0]
    return None


def _medication_key(name: str) -> str:
    """The drug name alone: strength and form words removed, letters and digits joined."""
    folded = _fold(name)
    if folded is None:
        return ""
    return NOT_LATIN_OR_DIGIT.sub("", STRENGTH_AND_FORM.sub(" ", folded))


def match_medication(spoken: str, medication_names: Sequence[str]) -> str | None:
    """The one listed name the spoken one means, or None. Two matches is None."""
    spoken_key = _medication_key(spoken)
    if not spoken_key:
        return None
    matches = [
        listed
        for listed in medication_names
        if (listed_key := _medication_key(listed))
        and jellyfish.damerau_levenshtein_distance(spoken_key, listed_key) <= MAX_MEDICATION_EDITS
    ]
    if len(matches) == 1:
        return matches[0]
    return None
