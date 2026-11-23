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

# At the variant level a name part needs at least this many letters: an initial never matches.
MIN_VARIANT_PART = 2
KEPT_UNITS = frozenset({"ch", "sh", "th"})
DOUBLED_LETTER = re.compile(r"(.)\1+")

UNITS = r"mg|mcg|g|ml|milligrams?|micrograms?|grams?|units?|iu"
STRENGTH = re.compile(rf"[0-9]+(?:{UNITS})?|(?:{UNITS})")
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
MEDICATION_TOKENS = re.compile(r"[a-z0-9]+")
# Two different tokens match only when both are letters only, at least this long, start with
# the same letter and are this many edits apart (insertion, deletion, substitution or swap).
MIN_CLOSE_TOKEN = 7
MAX_TOKEN_EDITS = 1


class NamedRecord(Protocol):
    @property
    def first_name(self) -> str: ...

    @property
    def last_name(self) -> str: ...


def normalize_name(full_name: str) -> tuple[str, str] | None:
    """(first, last): the first and last token once titles and suffixes are dropped.

    Lower-cased, accents folded, apostrophes join ("O'Brien" is one token), anything else
    outside a-z ends a token. Fewer than two tokens, more than MAX_NAME_LENGTH characters, or
    a letter that does not fold to a-z, is not a name.
    """
    if len(full_name) > MAX_NAME_LENGTH:
        return None
    decomposed = unicodedata.normalize("NFKD", full_name.lower())
    folded = "".join(char for char in decomposed if not unicodedata.combining(char))
    folded = folded.translate(APOSTROPHES)
    if NON_LATIN_LETTER.search(folded):
        return None
    tokens = [token for token in LATIN_WORDS.findall(folded) if token not in TITLES_AND_SUFFIXES]
    if len(tokens) < 2:
        return None
    return tokens[0], tokens[-1]


def _variant(part: str) -> str:
    """The spelling-variant form of one name part; the rules apply in this order."""
    text = part.replace("ph", "f").replace("ck", "k")
    units: list[str] = []
    index = 0
    while index < len(text):
        if text[index : index + 2] in KEPT_UNITS:
            units.append(text[index : index + 2])
            index += 2
        else:
            units.append(text[index])
            index += 1

    reduced: list[str] = []
    for position, unit in enumerate(units):
        following = units[position + 1] if position + 1 < len(units) else ""
        if unit == "c":
            unit = "s" if following in ("e", "i", "y") else "k"
        elif unit == "z":
            unit = "s"
        elif unit == "y":
            unit = "i"
        elif unit == "h" and position > 0:
            unit = ""
        reduced.append(unit)
    return DOUBLED_LETTER.sub(r"\1", "".join(reduced))


def name_key(full_name: str) -> str | None:
    """The variant form of (first, last), joined by a space: the key pick_patient compares."""
    name = normalize_name(full_name)
    if name is None:
        return None
    first, last = name
    return f"{_variant(first)} {_variant(last)}"


def _variant_match(claimed: tuple[str, str], recorded: tuple[str, str]) -> bool:
    if any(len(part) < MIN_VARIANT_PART for part in (*claimed, *recorded)):
        return False
    return (_variant(claimed[0]), _variant(claimed[1])) == (
        _variant(recorded[0]),
        _variant(recorded[1]),
    )


def pick_patient[R: NamedRecord](claimed_full_name: str, candidates: Iterable[R]) -> R | None:
    """The one candidate the claimed name belongs to, or None.

    Exactly one strict match (normalized first and last equal) wins. Otherwise exactly one
    variant match (the same spelling-variant form) wins. More than one match at the level
    being considered is None.
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

    variant = [
        candidate for candidate, name in named if name is not None and _variant_match(claimed, name)
    ]
    if len(variant) == 1:
        return variant[0]
    return None


def _medication_tokens(name: str) -> list[str]:
    """The drug-name tokens: strength, unit and form words dropped."""
    if len(name) > MAX_MEDICATION_LENGTH:
        return []
    lowered = name.lower()
    if NON_LATIN_LETTER.search(lowered):
        return []
    return [
        token
        for token in MEDICATION_TOKENS.findall(lowered)
        if not STRENGTH.fullmatch(token) and token not in FORM_WORDS
    ]


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
        return _tokens_match("".join(spoken), listed[0])
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
