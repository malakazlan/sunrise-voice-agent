from dataclasses import dataclass

from app.domain.matching import match_medication, name_key, normalize_name, pick_patient


@dataclass(frozen=True)
class Record:
    id: str
    first_name: str
    last_name: str


SARAH_JOHNSON = Record("p-sarah", "Sarah", "Johnson")
MARIA_GARCIA = Record("p-maria", "Maria", "Garcia")
MICHAEL_BROWN = Record("p-michael", "Michael", "Brown")


def wrong_picks(rows: list[tuple[str, list[Record], Record | None]]) -> list[str]:
    return [
        claimed
        for claimed, candidates, expected in rows
        if pick_patient(claimed, candidates) is not expected
    ]


def test_strict_name_matches() -> None:
    rows: list[tuple[str, list[Record], Record | None]] = [
        ("Sarah Johnson", [SARAH_JOHNSON], SARAH_JOHNSON),
        ("sarah johnson", [SARAH_JOHNSON], SARAH_JOHNSON),
        ("  Sarah   Johnson ", [SARAH_JOHNSON], SARAH_JOHNSON),
        ("Sarah-Johnson.", [SARAH_JOHNSON], SARAH_JOHNSON),
        ("Sarah Marie Johnson", [SARAH_JOHNSON], SARAH_JOHNSON),
        ("María García", [MARIA_GARCIA], MARIA_GARCIA),
    ]

    assert wrong_picks(rows) == []
    assert normalize_name("  Sarah   Johnson ") == ("sarah", "johnson")
    assert normalize_name("Sarah-Johnson.") == ("sarah", "johnson")
    assert normalize_name("Sarah Marie Johnson") == ("sarah", "johnson")
    assert normalize_name("María García") == ("maria", "garcia")


def test_phonetic_name_matches() -> None:
    rows: list[tuple[str, list[Record], Record | None]] = [
        ("Sara Johnson", [SARAH_JOHNSON], SARAH_JOHNSON),
        ("Sarah Jonson", [SARAH_JOHNSON], SARAH_JOHNSON),
        ("Sarah Johnsen", [SARAH_JOHNSON], SARAH_JOHNSON),
        ("Maria Garsia", [MARIA_GARCIA], MARIA_GARCIA),
        ("Micheal Brown", [MICHAEL_BROWN], MICHAEL_BROWN),
        ("Michael Browne", [MICHAEL_BROWN], MICHAEL_BROWN),
        # Same sound; acceptable because date of birth and last four already matched.
        ("Michael Braun", [MICHAEL_BROWN], MICHAEL_BROWN),
    ]

    assert wrong_picks(rows) == []


def test_names_that_must_not_match() -> None:
    rows: list[tuple[str, list[Record], Record | None]] = [
        ("Sandra Johnson", [SARAH_JOHNSON], None),
        ("Sarah Jackson", [SARAH_JOHNSON], None),
        ("Sarah Johnston", [SARAH_JOHNSON], None),
        ("Mary Johnson", [SARAH_JOHNSON], None),
        ("Johnson Sarah", [SARAH_JOHNSON], None),
        ("Sarah", [SARAH_JOHNSON], None),
        ("", [SARAH_JOHNSON], None),
        ("Michelle Brown", [MICHAEL_BROWN], None),
    ]

    assert wrong_picks(rows) == []


def test_no_candidates_means_no_match() -> None:
    assert pick_patient("Sarah Johnson", []) is None
    assert pick_patient("Sarah Johnson", ()) is None
    assert pick_patient("Sara Johnson", iter([])) is None
    # A name that is not among the candidates is not found anywhere else either.
    assert pick_patient("Sarah Johnson", [MARIA_GARCIA, MICHAEL_BROWN]) is None


def test_ambiguous_candidates_return_nothing() -> None:
    sara = Record("p-sara", "Sara", "Johnson")
    jonson = Record("p-jonson", "Sarah", "Jonson")
    duplicate = Record("p-sarah-2", "Sarah", "Johnson")

    assert pick_patient("Sarah Johnson", [sara, jonson]) is None
    assert pick_patient("Sarah Johnson", [SARAH_JOHNSON, duplicate]) is None
    assert pick_patient("Sara Johnson", [SARAH_JOHNSON, duplicate]) is None
    # Two strict matches are not resolved by falling through to the one other phonetic match.
    assert pick_patient("Sarah Johnson", [SARAH_JOHNSON, duplicate, sara]) is None


def test_strict_match_preferred() -> None:
    sara = Record("p-sara", "Sara", "Johnson")

    assert pick_patient("Sarah Johnson", [SARAH_JOHNSON, sara]) is SARAH_JOHNSON
    assert pick_patient("Sarah Johnson", [sara, SARAH_JOHNSON]) is SARAH_JOHNSON
    assert pick_patient("Sara Johnson", [SARAH_JOHNSON, sara]) is sara


def test_name_key_is_one_identity_per_sound() -> None:
    assert name_key("Sarah Johnson") == "SR JNSN"
    assert name_key("Sara Johnson") == "SR JNSN"
    assert name_key("sarah marie johnson") == "SR JNSN"
    assert name_key("Sarah Jonson") == "SR JNSN"
    assert name_key("Sandra Johnson") != name_key("Sarah Johnson")
    assert name_key("Sarah Johnston") != name_key("Sarah Johnson")
    assert name_key("Johnson Sarah") != name_key("Sarah Johnson")
    assert name_key("Sarah") is None
    assert name_key("") is None


def test_apostrophes_and_hyphens_normalize_the_same_on_both_sides() -> None:
    obrien = Record("p-obrien", "Sean", "O'Brien")
    smith_jones = Record("p-smith-jones", "Mary", "Smith-Jones")

    assert normalize_name("Sean O'Brien") == ("sean", "obrien")
    assert normalize_name("Sean O’Brien") == ("sean", "obrien")
    assert normalize_name("Sean OBrien") == ("sean", "obrien")
    assert normalize_name("Mary Smith-Jones") == ("mary", "jones")
    assert normalize_name("Mary Smith Jones") == ("mary", "jones")

    rows: list[tuple[str, list[Record], Record | None]] = [
        ("Sean O'Brien", [obrien], obrien),
        ("Sean O’Brien", [obrien], obrien),
        ("sean obrien", [obrien], obrien),
        ("Mary Smith-Jones", [smith_jones], smith_jones),
        ("Mary Smith Jones", [smith_jones], smith_jones),
        # Only the first and the last token count, as for a middle name.
        ("Mary Jones", [smith_jones], smith_jones),
        ("Mary Smith", [smith_jones], None),
    ]

    assert wrong_picks(rows) == []


def test_non_latin_and_empty_input_never_match() -> None:
    for claimed in ("", "   ", "--", ".", "Иван Петров", "王 小明", "Søren Johnson", "Sarah Ж"):
        assert normalize_name(claimed) is None, claimed
        assert name_key(claimed) is None, claimed
        assert pick_patient(claimed, [SARAH_JOHNSON]) is None, claimed

    assert pick_patient("Sarah Johnson", [Record("p-x", "Иван", "Петров")]) is None


def test_parts_without_a_sound_match_only_strictly() -> None:
    # The phonetic code of "W" and of "Y" is empty, so they cannot be told apart by sound.
    w_johnson = Record("p-w", "W", "Johnson")

    assert pick_patient("Y Johnson", [w_johnson]) is None
    assert pick_patient("W Johnson", [w_johnson]) is w_johnson


LISINOPRIL_AND_METFORMIN = ["Lisinopril 10mg", "Metformin 500mg"]
VAGUE_PHRASES = ["my medication", "the blood pressure one", ""]


def wrong_medications(rows: list[tuple[str, list[str], str | None]]) -> list[tuple[str, str]]:
    return [
        (spoken, ", ".join(names))
        for spoken, names, expected in rows
        if match_medication(spoken, names) != expected
    ]


def test_medications_that_match() -> None:
    rows: list[tuple[str, list[str], str | None]] = [
        ("lisinopril", LISINOPRIL_AND_METFORMIN, "Lisinopril 10mg"),
        ("Lisinopril 10 mg", LISINOPRIL_AND_METFORMIN, "Lisinopril 10mg"),
        ("lisinipril", LISINOPRIL_AND_METFORMIN, "Lisinopril 10mg"),
        ("lysinopril", LISINOPRIL_AND_METFORMIN, "Lisinopril 10mg"),
        ("metformin", LISINOPRIL_AND_METFORMIN, "Metformin 500mg"),
        ("metforman", LISINOPRIL_AND_METFORMIN, "Metformin 500mg"),
        ("met formin", LISINOPRIL_AND_METFORMIN, "Metformin 500mg"),
        ("levothyroxine", ["Levothyroxine 50mcg"], "Levothyroxine 50mcg"),
        ("levo thyroxin", ["Levothyroxine 50mcg"], "Levothyroxine 50mcg"),
        ("atorvastatin", ["Atorvastatin 20mg"], "Atorvastatin 20mg"),
        ("atorvastatine", ["Atorvastatin 20mg"], "Atorvastatin 20mg"),
    ]

    assert wrong_medications(rows) == []


def test_strength_and_form_words_are_ignored() -> None:
    rows: list[tuple[str, list[str], str | None]] = [
        ("Metformin 500 mg tablet", ["Metformin 500mg"], "Metformin 500mg"),
        ("levothyroxine 50 mcg tablets", ["Levothyroxine 50mcg"], "Levothyroxine 50mcg"),
        ("Lisinopril 10 milligrams", ["Lisinopril 10mg"], "Lisinopril 10mg"),
        ("atorvastatin", ["Atorvastatin 20 mg tablet"], "Atorvastatin 20 mg tablet"),
        ("metformin 2.5mg capsule", ["Metformin 500mg"], "Metformin 500mg"),
        ("10 mg", LISINOPRIL_AND_METFORMIN, None),
        ("tablet", LISINOPRIL_AND_METFORMIN, None),
    ]

    assert wrong_medications(rows) == []


def test_medications_that_must_not_match() -> None:
    rows: list[tuple[str, list[str], str | None]] = [
        ("metoprolol", ["Metformin 500mg"], None),
        ("losartan", ["Lisinopril 10mg"], None),
        ("simvastatin", ["Atorvastatin 20mg"], None),
        ("lipitor", ["Atorvastatin 20mg"], None),
    ]
    lists = [
        LISINOPRIL_AND_METFORMIN,
        ["Levothyroxine 50mcg"],
        ["Atorvastatin 20mg"],
        ["Metformin 500mg"],
        ["Lisinopril 10mg"],
    ]
    rows += [(phrase, names, None) for phrase in VAGUE_PHRASES for names in lists]
    rows += [
        (spoken, [], None)
        for spoken in ("lisinopril", "Lisinopril 10mg", "metformin", "my medication", "")
    ]
    # Different drugs whose names differ by two letters, from look-alike and sound-alike lists.
    rows += [
        ("escitalopram", ["Citalopram 20mg"], None),
        ("fosinopril", ["Lisinopril 10mg"], None),
        ("taxol", ["Paxil 20mg"], None),
        ("quinidine", ["Quinine 324mg"], None),
        ("prednisolone", ["Prednisone 10mg"], None),
        ("novolog", ["Novolin"], None),
        ("duloxetine", ["Fluoxetine 20mg"], None),
        ("lorazepam", ["Clonazepam 0.5mg"], None),
        ("folinic acid", ["Folic acid 1mg"], None),
    ]

    assert wrong_medications(rows) == []


def test_ambiguous_medication_returns_nothing() -> None:
    rows: list[tuple[str, list[str], str | None]] = [
        ("lisinopril", ["Lisinopril 10mg", "Lisinopril 20mg"], None),
        ("lisinipril", ["Lisinopril 10mg", "Lisinopril 20mg"], None),
        ("metformin", ["Metformin 500mg", "Metformin 1000mg", "Lisinopril 10mg"], None),
    ]

    assert wrong_medications(rows) == []


def test_blank_medication_names_never_match() -> None:
    # Nothing left after strength and form words are removed is never a drug name, on
    # either side, even next to a one-letter entry.
    rows: list[tuple[str, list[str], str | None]] = [
        ("", ["C 500mg"], None),
        ("10 mg", ["C 500mg"], None),
        ("a", ["10mg tablet"], None),
        ("tablets", ["Tablet 20mg"], None),
    ]

    assert wrong_medications(rows) == []


def test_non_latin_medication_input_never_matches() -> None:
    for spoken in ("лизиноприл", "立普妥", "lisinopril Ж"):
        assert match_medication(spoken, LISINOPRIL_AND_METFORMIN) is None, spoken
