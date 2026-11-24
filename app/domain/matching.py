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

# Longer input is not a name, or matches no medication. Checked before any other work.
MAX_NAME_LENGTH = 200
MAX_MEDICATION_LENGTH = 100

APOSTROPHES = str.maketrans("", "", "'’")
LATIN_WORDS = re.compile(r"[a-z]+")
# A letter (a word character that is not a digit or underscore) outside a-z.
NON_LATIN_LETTER = re.compile(r"[^\W\d_a-z]")
TITLES_AND_SUFFIXES = frozenset({"dr", "mr", "mrs", "ms", "miss", "jr", "sr", "ii", "iii", "iv"})

# A hyphen or slash between two letters or digits, at least one of them a digit, joins them:
# "b-12" is "b12", "70/30" is "7030".
DIGIT_JOINER = re.compile(r"(?<=[0-9])[-/](?=[a-z0-9])|(?<=[a-z0-9])[-/](?=[0-9])")
MEDICATION_TOKENS = re.compile(r"[a-z0-9]+")
UNITS = r"(?:mg|mcg|g|ml|milligrams?|micrograms?|grams?|units?|iu)"
UNIT_WORD = re.compile(UNITS)
NUMBER_WITH_UNIT = re.compile(rf"[0-9]+{UNITS}")
BARE_NUMBER = re.compile(r"[0-9]+")
FORM_WORDS = frozenset(
    {
        "tablet",
        "tablets",
        "tab",
        "tabs",
        "capsule",
        "capsules",
        "cap",
        "caps",
        "pill",
        "pills",
        "inhaler",
        "cream",
        "ointment",
        "solution",
        "injection",
        "patch",
        "drops",
        "spray",
    }
)
# Two different tokens match only when both are letters only, at least this long, start with
# the same letter and are this many edits apart (insertion, deletion, substitution or swap).
MIN_CLOSE_TOKEN = 7
MAX_TOKEN_EDITS = 1
# Spoken words are joined to meet a one-word listed name only when each has this many letters.
MIN_JOINED_WORD = 3


class NamedRecord(Protocol):
    @property
    def first_name(self) -> str: ...

    @property
    def last_name(self) -> str: ...


def normalize_name(full_name: str) -> tuple[str, ...] | None:
    """The name's tokens once titles and suffixes are dropped; None if none are left.

    Lower-cased, accents folded, apostrophes removed ("O'Brien" is one token), anything else
    outside a-z ends a token. More than MAX_NAME_LENGTH characters, or a letter that does not
    fold to a-z, is not a name.
    """
    if len(full_name) > MAX_NAME_LENGTH:
        return None
    decomposed = unicodedata.normalize("NFKD", full_name.lower())
    folded = "".join(char for char in decomposed if not unicodedata.combining(char))
    folded = folded.translate(APOSTROPHES)
    if NON_LATIN_LETTER.search(folded):
        return None
    tokens = tuple(
        token for token in LATIN_WORDS.findall(folded) if token not in TITLES_AND_SUFFIXES
    )
    return tokens or None


def name_key(full_name: str) -> str | None:
    """The normalized tokens joined by single spaces."""
    tokens = normalize_name(full_name)
    if tokens is None:
        return None
    return " ".join(tokens)


def _same_name(claimed: tuple[str, ...], first: tuple[str, ...], last: tuple[str, ...]) -> bool:
    # Some split of the claimed tokens into a non-empty front and back spells the first name
    # and the last name, so spacing inside either name does not matter and nothing is extra.
    first_letters, last_letters = "".join(first), "".join(last)
    return any(
        "".join(claimed[:split]) == first_letters and "".join(claimed[split:]) == last_letters
        for split in range(1, len(claimed))
    )


def pick_patient[R: NamedRecord](claimed_full_name: str, candidates: Iterable[R]) -> R | None:
    """The one candidate whose name the claimed name is, or None (none, or more than one)."""
    claimed = normalize_name(claimed_full_name)
    if claimed is None:
        return None
    matches = [
        candidate
        for candidate in candidates
        if (first := normalize_name(candidate.first_name)) is not None
        and (last := normalize_name(candidate.last_name)) is not None
        and _same_name(claimed, first, last)
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _medication_tokens(name: str) -> list[str]:
    """The drug-name tokens: strengths, unit words and form words dropped, numbers kept."""
    if len(name) > MAX_MEDICATION_LENGTH:
        return []
    lowered = name.lower()
    if NON_LATIN_LETTER.search(lowered):
        return []
    tokens = MEDICATION_TOKENS.findall(DIGIT_JOINER.sub("", lowered))

    kept: list[str] = []
    for position, token in enumerate(tokens):
        following = tokens[position + 1] if position + 1 < len(tokens) else ""
        if token in FORM_WORDS or UNIT_WORD.fullmatch(token) or NUMBER_WITH_UNIT.fullmatch(token):
            continue
        if BARE_NUMBER.fullmatch(token) and UNIT_WORD.fullmatch(following):
            continue
        kept.append(token)

    # A single letter and the bare number after it are one token: "b 12" is "b12".
    merged: list[str] = []
    for token in kept:
        if (
            merged
            and len(merged[-1]) == 1
            and merged[-1].isalpha()
            and BARE_NUMBER.fullmatch(token)
        ):
            merged[-1] += token
        else:
            merged.append(token)
    return merged


def _tokens_match(spoken: str, listed: str) -> bool:
    if spoken == listed:
        return True
    return (
        len(spoken) >= MIN_CLOSE_TOKEN
        and len(listed) >= MIN_CLOSE_TOKEN
        and spoken.isalpha()
        and listed.isalpha()
        and spoken[0] == listed[0]
        and jellyfish.damerau_levenshtein_distance(spoken, listed) <= MAX_TOKEN_EDITS
    )


def _entry_matches(spoken: list[str], listed: list[str]) -> bool:
    # Speech-to-text may split one word ("met formin"): joined, it is compared as one token.
    if len(listed) == 1 and len(spoken) > 1:
        return all(
            token.isalpha() and len(token) >= MIN_JOINED_WORD for token in spoken
        ) and _tokens_match("".join(spoken), listed[0])
    return len(spoken) == len(listed) and all(map(_tokens_match, spoken, listed))


def match_medication(spoken: str, medication_names: Sequence[str]) -> str | None:
    """The one listed name the spoken one means, or None. Zero or several matches is None."""
    spoken_tokens = _medication_tokens(spoken)
    if not spoken_tokens:
        return None
    matches = [
        listed
        for listed in medication_names
        if _entry_matches(spoken_tokens, _medication_tokens(listed))
    ]
    if len(matches) == 1:
        return matches[0]
    return None
