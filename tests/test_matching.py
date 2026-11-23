import time
from dataclasses import dataclass

from app.domain.matching import match_medication, name_key, normalize_name, pick_patient


@dataclass(frozen=True)
class Record:
    id: str
    first_name: str
    last_name: str


def person(first_name: str, last_name: str) -> Record:
    return Record(f"p-{first_name}-{last_name}".lower(), first_name, last_name)


SARAH_JOHNSON = person("Sarah", "Johnson")
MARIA_GARCIA = person("Maria", "Garcia")
MICHAEL_BROWN = person("Michael", "Brown")
JOHN_SMITH = person("John", "Smith")
JOHN_SMITH_JR = person("John", "Smith Jr")

# Longer than the 200-character cap, and a strict match for Sarah Johnson without it.
NAME_201 = "Sarah " + "m" * 187 + " Johnson"
NAME_200 = "Sarah " + "m" * 186 + " Johnson"


def wrong_picks(rows: list[tuple[str, Record]], expect_match: bool) -> list[str]:
    return [
        f"{claimed} / {candidate.first_name} {candidate.last_name}"
        for claimed, candidate in rows
        if (pick_patient(claimed, [candidate]) is candidate) is not expect_match
    ]


def test_strict_name_matches() -> None:
    rows = [
        ("Sarah Johnson", SARAH_JOHNSON),
        ("sarah johnson", SARAH_JOHNSON),
        ("  Sarah   Johnson ", SARAH_JOHNSON),
        ("Sarah-Johnson.", SARAH_JOHNSON),
        ("Sarah Marie Johnson", SARAH_JOHNSON),
        ("Dr Sarah Johnson", SARAH_JOHNSON),
        ("Sarah Johnson Jr", SARAH_JOHNSON),
        ("Mrs. Sarah Johnson", SARAH_JOHNSON),
        ("John Smith", JOHN_SMITH_JR),
        ("María García", MARIA_GARCIA),
        ("Phillip Lee", person("Phillip", "Lee")),
        ("Sean O'Brien", person("Sean", "O'Brien")),
        ("Sean O’Brien", person("Sean", "O'Brien")),
        ("Sean OBrien", person("Sean", "O'Brien")),
        (NAME_200, SARAH_JOHNSON),
    ]

    assert wrong_picks(rows, expect_match=True) == []
    # Strict: the normalized pairs are equal.
    unequal = [
        claimed
        for claimed, candidate in rows
        if normalize_name(claimed)
        != normalize_name(f"{candidate.first_name} {candidate.last_name}")
    ]
    assert unequal == []
    assert normalize_name("Mrs. Sarah Johnson Jr") == ("sarah", "johnson")
    assert normalize_name("Sean O’Brien") == ("sean", "obrien")


def test_variant_name_matches() -> None:
    rows = [
        ("Sara Johnson", SARAH_JOHNSON),
        ("Sarah Jonson", SARAH_JOHNSON),
        ("Jon Smith", JOHN_SMITH),
        ("Maria Garsia", MARIA_GARCIA),
        ("John Smyth", JOHN_SMITH),
        ("Bryan Carter", person("Brian", "Carter")),
        ("Philip Lee", person("Phillip", "Lee")),
        ("Katherine Brown", person("Catherine", "Brown")),
        ("Maria Gonzales", person("Maria", "Gonzalez")),
        # Pins rule 1 (ph to f): without it Sophia reduces to "sopia".
        ("Sofia Garcia", person("Sophia", "Garcia")),
    ]

    assert wrong_picks(rows, expect_match=True) == []
    # Variant, not strict: the normalized pairs differ.
    equal = [
        claimed
        for claimed, candidate in rows
        if normalize_name(claimed)
        == normalize_name(f"{candidate.first_name} {candidate.last_name}")
    ]
    assert equal == []


def test_names_that_must_not_match() -> None:
    paul, eric, carl = person("Paul", "Smith"), person("Eric", "Smith"), person("Carl", "Smith")
    daniel, louis = person("Daniel", "Smith"), person("Louis", "Smith")
    francis, jane = person("Francis", "Smith"), person("Jane", "Smith")
    dean, tom, kevin = person("Dean", "Smith"), person("Tom", "Smith"), person("Kevin", "Smith")
    lee, joe, bo = person("Lee", "Smith"), person("Joe", "Smith"), person("Bo", "Smith")
    raj, carter = person("Raj", "Patel"), person("James", "Carter")
    martin, miller = person("Ann", "Martin"), person("Ann", "Miller")
    ann_brown, wei = person("Ann", "Brown"), person("Wei", "Chen")
    mei, min_kim = person("Mei", "Lin"), person("Min", "Kim")
    ho, ann_lee, nguyen = person("Ann", "Ho"), person("Ann", "Lee"), person("Emily", "Nguyen")
    j_smith = person("J", "Smith")
    rows = [
        ("Sandra Johnson", SARAH_JOHNSON),
        ("Sarah Jackson", SARAH_JOHNSON),
        ("Sarah Johnston", SARAH_JOHNSON),
        ("Mary Johnson", SARAH_JOHNSON),
        ("Johnson Sarah", SARAH_JOHNSON),
        ("Sarah Johnsen", SARAH_JOHNSON),
        ("Michelle Brown", MICHAEL_BROWN),
        ("Micheal Brown", MICHAEL_BROWN),
        ("Michael Browne", MICHAEL_BROWN),
        ("Michael Braun", MICHAEL_BROWN),
        ("Paula Smith", paul),
        ("Erica Smith", eric),
        ("Carla Smith", carl),
        ("Carol Smith", carl),
        ("Daniela Smith", daniel),
        ("Danielle Smith", daniel),
        ("Louise Smith", louis),
        ("Lois Smith", louis),
        ("Frances Smith", francis),
        ("Joan Smith", JOHN_SMITH),
        ("Jean Smith", JOHN_SMITH),
        ("Juan Smith", JOHN_SMITH),
        ("Jan Smith", JOHN_SMITH),
        ("Jen Smith", JOHN_SMITH),
        ("June Smith", jane),
        ("Mario Garcia", MARIA_GARCIA),
        ("Marie Garcia", MARIA_GARCIA),
        ("Mary Garcia", MARIA_GARCIA),
        ("Diane Smith", dean),
        ("Dawn Smith", dean),
        ("Don Smith", dean),
        ("Dina Smith", dean),
        ("Tim Smith", tom),
        ("Tam Smith", tom),
        ("Gavin Smith", kevin),
        ("Leo Smith", lee),
        ("Jay Smith", joe),
        ("Joy Smith", joe),
        ("Raj Patil", raj),
        ("Maria Gracia", MARIA_GARCIA),
        ("James Cartier", carter),
        ("James Crater", carter),
        ("James Carver", carter),
        ("Ann Morton", martin),
        ("Ann Mailer", miller),
        ("Ann Brian", ann_brown),
        ("Wei Chan", wei),
        ("Wei Chin", wei),
        ("Wei Chun", wei),
        ("Mei Lan", mei),
        ("Mei Len", mei),
        ("Mei Lane", mei),
        ("Min Kam", min_kim),
        ("Min Kum", min_kim),
        ("Ann Hu", ho),
        ("Ann Loy", ann_lee),
        ("Ann Low", ann_lee),
        ("Ann Lau", ann_lee),
        ("Ann Li", ann_lee),
        ("Ann Leigh", ann_lee),
        ("Emily Newton", nguyen),
        ("Emily Wen", nguyen),
        ("John Schmidt", JOHN_SMITH),
        ("J Smith", joe),
        ("B Smith", bo),
        ("Jo Smith", j_smith),
        ("John Doe Jr", JOHN_SMITH_JR),
        ("John Jr", JOHN_SMITH_JR),
        # ch, sh and th are kept as units: Chen is not Ken, Smith is not Smit.
        ("Wei Ken", wei),
        ("John Smit", JOHN_SMITH),
        # A first-letter h is kept: Hanna is not Anna, Helen is not Ellen.
        ("Hanna Smith", person("Anna", "Smith")),
        ("Helen Smith", person("Ellen", "Smith")),
        # Initials only ever match exactly, even where the rules would make them equal.
        ("C Smith", person("K", "Smith")),
        ("Z Smith", person("S", "Smith")),
        ("Y Smith", person("I", "Smith")),
    ]
    for claimed in ("Sarah", "", "Dr", "Jr Jr", NAME_201):
        rows += [
            (claimed, candidate)
            for candidate in (SARAH_JOHNSON, person("Sarah", "Sarah"), JOHN_SMITH_JR)
        ]

    assert wrong_picks(rows, expect_match=False) == []


def test_no_candidates_means_no_match() -> None:
    assert pick_patient("Sarah Johnson", []) is None
    assert pick_patient("Sarah Johnson", ()) is None
    assert pick_patient("Sara Johnson", iter([])) is None
    # A name that is not among the candidates is not found anywhere else either.
    assert pick_patient("Sarah Johnson", [MARIA_GARCIA, MICHAEL_BROWN]) is None


def test_ambiguous_candidates_return_nothing() -> None:
    sara = person("Sara", "Johnson")
    jonson = person("Sarah", "Jonson")
    duplicate = Record("p-sarah-2", "Sarah", "Johnson")

    assert pick_patient("Sarah Johnson", [sara, jonson]) is None
    assert pick_patient("Sarah Johnson", [SARAH_JOHNSON, duplicate]) is None
    assert pick_patient("Sara Johnson", [SARAH_JOHNSON, duplicate]) is None
    # Two strict matches are not resolved by falling through to the one other variant match.
    assert pick_patient("Sarah Johnson", [SARAH_JOHNSON, duplicate, sara]) is None
    assert pick_patient("Sarah", [person("Sarah", "Sarah")]) is None


def test_strict_match_preferred() -> None:
    sara = person("Sara", "Johnson")

    assert pick_patient("Sarah Johnson", [SARAH_JOHNSON, sara]) is SARAH_JOHNSON
    assert pick_patient("Sarah Johnson", [sara, SARAH_JOHNSON]) is SARAH_JOHNSON
    assert pick_patient("Sara Johnson", [SARAH_JOHNSON, sara]) is sara


def test_name_key_is_the_variant_form() -> None:
    assert name_key("Sarah Johnson") == "sara jonson"
    assert name_key("Sara Johnson") == "sara jonson"
    assert name_key("Dr Sarah Marie Johnson Jr") == "sara jonson"
    assert name_key("Phillip Lee") == "filip le"
    assert name_key("Katherine Brown") == "katherine brown"
    assert name_key("Catherine Brown") == "katherine brown"
    assert name_key("Maria Gonzalez") == "maria gonsales"
    assert name_key("John Smyth") == "jon smith"
    assert name_key("Sarah Johnsen") != name_key("Sarah Johnson")
    assert name_key("Paula Smith") != name_key("Paul Smith")
    assert name_key("Johnson Sarah") != name_key("Sarah Johnson")
    for not_a_name in ("Sarah", "", "Dr", "Jr Jr", NAME_201):
        assert name_key(not_a_name) is None, not_a_name


def test_hyphenated_last_names_use_the_last_token() -> None:
    smith_jones = person("Mary", "Smith-Jones")

    assert normalize_name("Mary Smith-Jones") == ("mary", "jones")
    assert pick_patient("Mary Smith Jones", [smith_jones]) is smith_jones
    assert pick_patient("Mary Jones", [smith_jones]) is smith_jones
    assert pick_patient("Mary Smith", [smith_jones]) is None


def test_non_latin_and_empty_input_never_match() -> None:
    for claimed in ("", "   ", "--", ".", "Иван Петров", "王 小明", "Søren Johnson", "Sarah Ж"):
        assert normalize_name(claimed) is None, claimed
        assert name_key(claimed) is None, claimed
        assert pick_patient(claimed, [SARAH_JOHNSON]) is None, claimed

    assert pick_patient("Sarah Johnson", [person("Иван", "Петров")]) is None


def test_long_input_returns_quickly() -> None:
    # One character that Unicode normalization expands eighteen-fold.
    pathological = "ﷺ" * 200_000
    started = time.perf_counter()
    assert pick_patient(pathological, [SARAH_JOHNSON]) is None
    assert name_key(pathological) is None
    assert match_medication(pathological, ["Lisinopril 10mg"]) is None
    assert match_medication("1a " * 66_667, ["Lisinopril 10mg"]) is None
    elapsed = time.perf_counter() - started

    assert elapsed < 0.05, f"{elapsed * 1000:.1f} ms"


LISINOPRIL_AND_METFORMIN = ["Lisinopril 10mg", "Metformin 500mg"]
METFORMIN_AND_ER = ["Metformin 500mg", "Metformin ER 500mg"]

# Longer than the 100-character cap, and a match for Lisinopril 10mg without it.
SPOKEN_101 = "lisinopril" + " " * 91
SPOKEN_100 = "lisinopril" + " " * 90


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
        ("lisinopril 10 milligrams", LISINOPRIL_AND_METFORMIN, "Lisinopril 10mg"),
        ("lisinipril", LISINOPRIL_AND_METFORMIN, "Lisinopril 10mg"),
        ("lysinopril", LISINOPRIL_AND_METFORMIN, "Lisinopril 10mg"),
        ("metformin", LISINOPRIL_AND_METFORMIN, "Metformin 500mg"),
        ("metforman", LISINOPRIL_AND_METFORMIN, "Metformin 500mg"),
        ("met formin", LISINOPRIL_AND_METFORMIN, "Metformin 500mg"),
        ("metformin tablets", LISINOPRIL_AND_METFORMIN, "Metformin 500mg"),
        ("levothyroxine", ["Levothyroxine 50mcg"], "Levothyroxine 50mcg"),
        ("levo thyroxin", ["Levothyroxine 50mcg"], "Levothyroxine 50mcg"),
        ("atorvastatin", ["Atorvastatin 20mg"], "Atorvastatin 20mg"),
        ("atorvastatine", ["Atorvastatin 20mg"], "Atorvastatin 20mg"),
        ("albuterol", ["Albuterol inhaler"], "Albuterol inhaler"),
        ("albuterol inhaler", ["Albuterol inhaler"], "Albuterol inhaler"),
        ("metformin er", METFORMIN_AND_ER, "Metformin ER 500mg"),
        ("metformin", METFORMIN_AND_ER, "Metformin 500mg"),
        ("vitamin b12", ["Vitamin B12 1000mcg"], "Vitamin B12 1000mcg"),
        # Seven letters, one edit: the shortest token that may differ.
        ("lipitar", ["Lipitor 20mg"], "Lipitor 20mg"),
        ("humulin n", ["Humulin N", "Humulin R"], "Humulin N"),
        (SPOKEN_100, LISINOPRIL_AND_METFORMIN, "Lisinopril 10mg"),
    ]

    assert wrong_medications(rows) == []


def test_strength_and_form_words_are_ignored() -> None:
    rows: list[tuple[str, list[str], str | None]] = [
        ("Metformin 500 mg tablet", ["Metformin 500mg"], "Metformin 500mg"),
        ("levothyroxine 50 mcg tablets", ["Levothyroxine 50mcg"], "Levothyroxine 50mcg"),
        ("atorvastatin", ["Atorvastatin 20 mg tablet"], "Atorvastatin 20 mg tablet"),
        ("metformin 2.5mg capsule", ["Metformin 500mg"], "Metformin 500mg"),
        ("insulin 10 units injection", ["Insulin"], "Insulin"),
        ("hydrocortisone cream", ["Hydrocortisone 1% cream"], "Hydrocortisone 1% cream"),
        ("timolol drops", ["Timolol 0.5% solution"], "Timolol 0.5% solution"),
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
        # One edit, but shorter than seven letters.
        ("plavex", ["Plavix 75mg"], None),
        ("paxel", ["Paxil 20mg"], None),
        # One edit, but the tokens mix letters and digits.
        ("vitaminb1", ["VitaminB12 1000mcg"], None),
        ("humulin r", ["Humulin N"], None),
        ("novolin r", ["Novolin N"], None),
        ("vitamin e", ["Vitamin D 50mcg"], None),
        ("vitamin k", ["Vitamin D 50mcg"], None),
        ("vitamin c", ["Vitamin D 50mcg"], None),
        ("vitamin b1", ["Vitamin B12 1000mcg"], None),
        ("iodine", ["Lodine 400mg"], None),
        ("ziac", ["Zinc 50mg"], None),
        ("videx", ["Bidex 400mg"], None),
        ("some", ["Soma 350mg"], None),
        ("a", ["C 500mg"], None),
        ("metformin", ["Metformin ER 500mg"], None),
        ("metformin er", ["Metformin 500mg"], None),
        # Filler words are not handled.
        ("my albuterol", ["Albuterol inhaler"], None),
        ("the metformin one", LISINOPRIL_AND_METFORMIN, None),
        ("lisinopril please", LISINOPRIL_AND_METFORMIN, None),
        # Different drugs whose names differ by two letters.
        ("escitalopram", ["Citalopram 20mg"], None),
        ("fosinopril", ["Lisinopril 10mg"], None),
        ("taxol", ["Paxil 20mg"], None),
        ("quinidine", ["Quinine 324mg"], None),
        ("prednisolone", ["Prednisone 10mg"], None),
        ("novolog", ["Novolin"], None),
        ("duloxetine", ["Fluoxetine 20mg"], None),
        ("lorazepam", ["Clonazepam 0.5mg"], None),
        ("folinic acid", ["Folic acid 1mg"], None),
        # The first letter must agree (xylenol is an antiseptic).
        ("xylenol", ["Tylenol 500mg"], None),
    ]
    lists = [
        LISINOPRIL_AND_METFORMIN,
        METFORMIN_AND_ER,
        ["Levothyroxine 50mcg"],
        ["Atorvastatin 20mg"],
        ["Albuterol inhaler"],
        ["Metformin 500mg"],
        ["Lisinopril 10mg"],
        ["Inhaler"],
        ["Tablet 20mg"],
    ]
    vague = ["my medication", "the blood pressure one", "the inhaler", "inhaler", "10 mg"]
    vague += ["tablets", ""]
    rows += [(phrase, names, None) for phrase in vague for names in lists]
    rows += [
        (spoken, [], None)
        for spoken in ("lisinopril", "Lisinopril 10mg", "metformin", "my medication", "")
    ]
    rows += [(SPOKEN_101, names, None) for names in lists]

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
