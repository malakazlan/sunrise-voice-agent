import time
from dataclasses import dataclass

from app.domain.matching import (
    NormalizedName,
    match_medication,
    name_key,
    normalize_name,
    pick_patient,
)


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
ANN_LEE = person("Ann", "Lee")

# Longer than the 200-character cap, and a match for Sarah Johnson without it.
NAME_201 = "Sarah" + " " * 189 + "Johnson"
NAME_200 = "Sarah" + " " * 188 + "Johnson"


def wrong_picks(rows: list[tuple[str, Record]], expect_match: bool) -> list[str]:
    return [
        f"{claimed} / {candidate.first_name} {candidate.last_name}"
        for claimed, candidate in rows
        if (pick_patient(claimed, [candidate]) is candidate) is not expect_match
    ]


O_CONNOR = person("Ann", "O'Connor")
SMITH_JONES = person("Mary", "Smith-Jones")
DE_LA_CRUZ = person("Maria", "De La Cruz")
MARY_ANN = person("Mary Ann", "Lee")
MCDONALD = person("John", "McDonald")
NAMES_THAT_MATCH = [
    ("Sarah Johnson", SARAH_JOHNSON),
    ("sarah johnson", SARAH_JOHNSON),
    ("  Sarah   Johnson ", SARAH_JOHNSON),
    ("Sarah-Johnson.", SARAH_JOHNSON),
    ("Dr Sarah Johnson", SARAH_JOHNSON),
    ("Sarah Johnson Jr", SARAH_JOHNSON),
    ("Mrs. Sarah Johnson", SARAH_JOHNSON),
    ("John Smith", JOHN_SMITH_JR),
    ("María García", MARIA_GARCIA),
    ("Ann O'Connor", O_CONNOR),
    ("Ann O\u2019Connor", O_CONNOR),
    ("Ann OConnor", O_CONNOR),
    ("Ann O Connor", O_CONNOR),
    ("Mary Smith-Jones", SMITH_JONES),
    ("Mary Smith Jones", SMITH_JONES),
    ("Mary SmithJones", SMITH_JONES),
    ("Maria De La Cruz", DE_LA_CRUZ),
    ("Maria Delacruz", DE_LA_CRUZ),
    ("Mary Ann Lee", MARY_ANN),
    ("Maryann Lee", MARY_ANN),
    ("Mary-Ann Lee", MARY_ANN),
    ("John Mc Donald", MCDONALD),
    ("John McDonald", MCDONALD),
    (NAME_200, SARAH_JOHNSON),
    ("S arah Johnson", SARAH_JOHNSON),
    ("Sar ah John son", SARAH_JOHNSON),
    ("Sar\u00adah Johnson", SARAH_JOHNSON),
    ("John Smith Jr", JOHN_SMITH_JR),
    ("John Smith Jr", JOHN_SMITH),
    ("John Smith III", person("John", "Smith III")),
    # A suffix stored with the first name is the same suffix.
    ("John Smith Jr", person("John Jr", "Smith")),
    ("John Smith", person("John Jr", "Smith")),
]


def test_names_that_match() -> None:
    assert wrong_picks(NAMES_THAT_MATCH, expect_match=True) == []


def test_names_that_must_not_match() -> None:
    sarah = [
        "Sara Johnson",
        "Sarah Jonson",
        "Sarah Johnsen",
        "Sandra Johnson",
        "Sarah Jackson",
        "Sarah Johnston",
        "Mary Johnson",
        "Johnson Sarah",
        "Zara Johnson",
        "Zahra Johnson",
        "Sarah Marie Johnson",
    ]
    john = ["Jon Smith", "Joan Smith", "Jean Smith", "Juan Smith", "Jan Smith", "Jen Smith"]
    john += ["Johan Smith", "John Smyth", "John Schmidt", "John Smit"]
    michael = ["Michelle Brown", "Micheal Brown", "Michael Browne", "Michael Braun"]
    maria = ["Maria Garsia", "Maria Gracia", "Mario Garcia", "Marie Garcia", "Mary Garcia"]
    rows = [(claimed, SARAH_JOHNSON) for claimed in sarah]
    rows += [(claimed, JOHN_SMITH) for claimed in john]
    rows += [(claimed, MICHAEL_BROWN) for claimed in michael]
    rows += [(claimed, MARIA_GARCIA) for claimed in maria]
    rows += [
        ("Maria Gonzales", person("Maria", "Gonzalez")),
        ("Bryan Carter", person("Brian", "Carter")),
        ("Philip Lee", person("Phillip", "Lee")),
        ("Katherine Brown", person("Catherine", "Brown")),
        ("Sofia Garcia", person("Sophia", "Garcia")),
    ]
    smith_first_names = [
        ("Paula", "Paul"),
        ("Erica", "Eric"),
        ("Carla", "Carl"),
        ("Carol", "Carl"),
        ("Daniela", "Daniel"),
        ("Danielle", "Daniel"),
        ("Louise", "Louis"),
        ("Lois", "Louis"),
        ("Frances", "Francis"),
        ("June", "Jane"),
        ("Diane", "Dean"),
        ("Dawn", "Dean"),
        ("Don", "Dean"),
        ("Dina", "Dean"),
        ("Tim", "Tom"),
        ("Tam", "Tom"),
        ("Gavin", "Kevin"),
        ("Leo", "Lee"),
        ("Jay", "Joe"),
        ("Joy", "Joe"),
        ("Hanna", "Anna"),
        ("Helen", "Ellen"),
    ]
    rows += [(f"{said} Smith", person(record, "Smith")) for said, record in smith_first_names]
    rows += [
        ("Raj Patil", person("Raj", "Patel")),
        ("James Cartier", person("James", "Carter")),
        ("James Crater", person("James", "Carter")),
        ("James Carver", person("James", "Carter")),
        ("Ann Morton", person("Ann", "Martin")),
        ("Ann Mailer", person("Ann", "Miller")),
        ("Ann Brian", person("Ann", "Brown")),
        ("Wei Chan", person("Wei", "Chen")),
        ("Wei Chin", person("Wei", "Chen")),
        ("Wei Chun", person("Wei", "Chen")),
        ("Wei Ken", person("Wei", "Chen")),
        ("Mei Lan", person("Mei", "Lin")),
        ("Mei Len", person("Mei", "Lin")),
        ("Mei Lane", person("Mei", "Lin")),
        ("Mei Linh", person("Mei", "Lin")),
        ("Mei Lynn", person("Mei", "Lin")),
        ("Min Kam", person("Min", "Kim")),
        ("Min Kum", person("Min", "Kim")),
        ("Minh Kim", person("Min", "Kim")),
        ("Ann Hu", person("Ann", "Ho")),
    ]
    for said in ("Ann Le", "Ann Loy", "Ann Low", "Ann Lau", "Ann Li", "Ann Leigh"):
        rows.append((said, ANN_LEE))
    rows += [("Anh Lee", ANN_LEE), ("An Lee", ANN_LEE)]
    ann_last_names = [
        ("Zhu", "Su"),
        ("Zhong", "Song"),
        ("Zhang", "Sang"),
        ("Khan", "Kan"),
        ("Kahn", "Kan"),
        ("Can", "Kan"),
        ("Hahn", "Han"),
        ("Hanh", "Han"),
        ("Choo", "Cho"),
        ("Koo", "Ko"),
        ("Yoo", "Yo"),
        ("Dhillon", "Dillon"),
        ("Rahman", "Raman"),
        ("Mehta", "Meta"),
        ("Cao", "Kao"),
        ("Cohen", "Koen"),
    ]
    rows += [(f"Ann {said}", person("Ann", record)) for said, record in ann_last_names]
    lee_first_names = [("May", "Mai"), ("Kay", "Kai"), ("Ray", "Rai"), ("Rahul", "Raul")]
    lee_first_names += [("Yan", "Ian")]
    rows += [(f"{said} Lee", person(record, "Lee")) for said, record in lee_first_names]
    rows += [
        ("Emily Newton", person("Emily", "Nguyen")),
        ("Emily Wen", person("Emily", "Nguyen")),
        ("Maria Cruz", person("Maria", "De La Cruz")),
        ("Juan De Leon", person("Juan", "Leon")),
        ("Ann O Connor", person("Ann", "Connor")),
        ("Ann OConnor", person("Ann", "Connor")),
        ("John Mc Donald", person("John", "Donald")),
        ("Ann Van Dyke", person("Ann", "Dyke")),
        ("Jo Anderson", person("Joan", "Derson")),
        ("Mary Jones", person("Mary", "Smith-Jones")),
        ("J Smith", person("Joe", "Smith")),
        ("B Smith", person("Bo", "Smith")),
        ("Jo Smith", person("J", "Smith")),
        ("John Doe Jr", JOHN_SMITH_JR),
        ("John Jr", JOHN_SMITH_JR),
        ("John Smith Sr", JOHN_SMITH_JR),
        ("John Smith Jr", person("John", "Smith Sr")),
        ("John Smith III", person("John", "Smith II")),
        ("John Smith II", person("John", "Smith III")),
        ("John Smith Jr Sr", JOHN_SMITH_JR),
        ("John Smith Sr", person("John Jr", "Smith")),
        # A record whose two fields carry different suffixes is not a name.
        ("John Smith", person("John Jr", "Smith Sr")),
        ("John Smith Jr", person("John Jr", "Smith Sr")),
        ("C Smith", person("K", "Smith")),
        ("Z Smith", person("S", "Smith")),
        ("Y Smith", person("I", "Smith")),
        ("Søren Johnson", person("S", "Johnson")),
    ]
    any_record = [SARAH_JOHNSON, person("Sarah", "Sarah"), JOHN_SMITH_JR, person("S", "Johnson")]
    for claimed in ("Sarah", "", "Dr", "Jr Jr", NAME_201, "Søren Johnson"):
        rows += [(claimed, candidate) for candidate in any_record]

    assert wrong_picks(rows, expect_match=False) == []


def test_no_candidates_means_no_match() -> None:
    assert pick_patient("Sarah Johnson", []) is None
    assert pick_patient("Sarah Johnson", ()) is None
    assert pick_patient("Sarah Johnson", iter([])) is None
    # A name that is not among the candidates is not found anywhere else either.
    assert pick_patient("Sarah Johnson", [MARIA_GARCIA, MICHAEL_BROWN]) is None


def test_ambiguous_candidates_return_nothing() -> None:
    duplicate = Record("p-sarah-2", "Sarah", "Johnson")
    spaced = person("Sarah", "John Son")

    assert pick_patient("Sarah Johnson", [SARAH_JOHNSON, duplicate]) is None
    assert pick_patient("Sarah Johnson", [SARAH_JOHNSON, spaced]) is None
    assert pick_patient("Sarah", [person("Sarah", "Sarah")]) is None


def test_one_match_among_several_is_that_match() -> None:
    sara = person("Sara", "Johnson")
    jonson = person("Sarah", "Jonson")

    assert pick_patient("Sarah Johnson", [sara, SARAH_JOHNSON, jonson]) is SARAH_JOHNSON
    assert pick_patient("Sara Johnson", [SARAH_JOHNSON, jonson, sara]) is sara
    assert pick_patient("Sarah Jonson", [jonson, sara, SARAH_JOHNSON]) is jonson


def test_name_key_is_the_letters_without_separators() -> None:
    for claimed in ("Sarah Johnson", "S arah Johnson", "Sar ah John son", "Sar\u00adah Johnson"):
        assert name_key(claimed) == "sarahjohnson", claimed
    assert name_key("Dr. Sarah Johnson Jr") == "sarahjohnson"
    assert name_key("María García") == "mariagarcia"
    assert name_key("Jo Anderson") == "joanderson"
    assert name_key("Joan Derson") == "joanderson"
    assert name_key("Sara Johnson") != name_key("Sarah Johnson")
    for not_a_name in ("", "   ", "Dr", "Jr Jr", NAME_201, "Søren Johnson", "John Smith Jr Sr"):
        assert name_key(not_a_name) is None, not_a_name


def test_a_matching_name_has_the_record_key() -> None:
    different = [
        claimed
        for claimed, record in NAMES_THAT_MATCH
        if name_key(claimed) != name_key(f"{record.first_name} {record.last_name}")
    ]

    assert different == []


def test_normalize_name_gives_every_token_and_the_suffix() -> None:
    assert normalize_name("Mrs. Mary-Ann  O'Connor Jr") == NormalizedName(
        ("mary", "ann", "oconnor"), "jr"
    )
    assert normalize_name("Sarah") == NormalizedName(("sarah",), None)
    assert normalize_name("John III Smith") == NormalizedName(("john", "smith"), "iii")
    assert normalize_name("John Smith Jr Jr") == NormalizedName(("john", "smith"), "jr")
    assert normalize_name("John Smith Jr Sr") is None
    assert normalize_name("Dr Jr") is None


def test_non_latin_and_empty_input_never_match() -> None:
    for claimed in ("", "   ", "--", ".", "Иван Петров", "王 小明", "Søren Johnson", "Sarah Ж"):
        assert normalize_name(claimed) is None, claimed
        assert name_key(claimed) is None, claimed
        assert pick_patient(claimed, [SARAH_JOHNSON]) is None, claimed

    assert pick_patient("Sarah Johnson", [person("Иван", "Петров")]) is None


def test_long_input_returns_quickly() -> None:
    # One character that Unicode normalization expands eighteen-fold.
    pathological = "\ufdfa" * 200_000
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
        ("insulin 10 units injection", ["Insulin"], "Insulin"),
        ("vitamin d", ["Vitamin D 2000 IU"], "Vitamin D 2000 IU"),
        # The strength is dropped before a letter and a number merge.
        ("vitamin c", ["Vitamin C 500 mg"], "Vitamin C 500 mg"),
        ("humulin r u 100 units", ["Humulin R U 500 units/mL"], "Humulin R U 500 units/mL"),
        ("10 mg", LISINOPRIL_AND_METFORMIN, None),
        ("tablet", LISINOPRIL_AND_METFORMIN, None),
    ]

    assert wrong_medications(rows) == []


def test_decimal_and_percent_strengths_are_dropped() -> None:
    hydrocortisone = ["Hydrocortisone 1% cream"]
    timolol = ["Timolol 0.5% solution"]
    rows: list[tuple[str, list[str], str | None]] = [
        ("clonazepam", ["Clonazepam 0.5mg"], "Clonazepam 0.5mg"),
        ("clonazepam 0.5 mg", ["Clonazepam 0.5mg"], "Clonazepam 0.5mg"),
        ("hydrocortisone cream", hydrocortisone, "Hydrocortisone 1% cream"),
        ("hydrocortisone", hydrocortisone, "Hydrocortisone 1% cream"),
        ("hydrocortisone 1 percent", hydrocortisone, "Hydrocortisone 1% cream"),
        ("timolol drops", timolol, "Timolol 0.5% solution"),
        ("timolol", timolol, "Timolol 0.5% solution"),
        ("metformin", ["Metformin 2.5mg capsule"], "Metformin 2.5mg capsule"),
        ("metformin 2.5mg capsule", ["Metformin 500mg"], "Metformin 500mg"),
        ("hydrocortisone 1 %", hydrocortisone, "Hydrocortisone 1% cream"),
    ]

    assert wrong_medications(rows) == []


def test_numbers_without_a_unit_are_kept() -> None:
    # A number, decimals included, that no unit marks as a strength must match exactly.
    rows: list[tuple[str, list[str], str | None]] = [
        ("clonazepam 0.5", ["Clonazepam 0.5mg"], None),
        ("clonazepam 1", ["Clonazepam 0.5mg"], None),
        ("lisinopril 20", ["Lisinopril 10mg"], None),
    ]

    assert wrong_medications(rows) == []


def test_medications_with_numbers_that_match() -> None:
    b12 = ["Vitamin B-12 1000mcg"]
    vitamin_d = ["Vitamin D-3 2000 IU", "Vitamin D 50mcg"]
    humulin = ["Humulin 70/30", "Humulin N"]
    rows: list[tuple[str, list[str], str | None]] = [
        ("vitamin b12", b12, "Vitamin B-12 1000mcg"),
        ("vitamin b 12", b12, "Vitamin B-12 1000mcg"),
        ("vitamin b-12", b12, "Vitamin B-12 1000mcg"),
        ("vitamin d3", vitamin_d, "Vitamin D-3 2000 IU"),
        ("vitamin d 3", vitamin_d, "Vitamin D-3 2000 IU"),
        ("vitamin d", vitamin_d, "Vitamin D 50mcg"),
        ("humulin 70/30", humulin, "Humulin 70/30"),
        # A number spoken apart from its word stays a separate token.
        ("humulin 70 30", humulin, None),
        ("omega 3", ["Omega-3"], None),
        ("humulin r u-500", ["Humulin R U-500"], "Humulin R U-500"),
        ("humulin r u 500", ["Humulin R U-500"], "Humulin R U-500"),
        ("omega-3", ["Omega-3"], "Omega-3"),
    ]

    assert wrong_medications(rows) == []


def test_medications_with_numbers_that_must_not_match() -> None:
    three = ["Seroquel 25mg", "Lamictal 100mg", "Depakote 250mg"]
    rows: list[tuple[str, list[str], str | None]] = [
        ("humulin r u 100", ["Humulin R U-500"], None),
        ("humulin r u-100", ["Humulin R U-500"], None),
        ("humulin r", ["Humulin R U-500"], None),
        ("humulin r", ["Humulin 70/30"], None),
        ("humulin n", ["Humulin 70/30"], None),
        ("humulin", ["Humulin 70/30"], None),
        ("novolin r", ["Novolin 70/30"], None),
        ("seroquel x", three, None),
        ("lamictal x", three, None),
        ("depakote r", three, None),
        ("humalog mix 50/50", ["Humalog Mix 75/25"], None),
        ("vitamin b 6", ["Vitamin B-12 1000mcg"], None),
        ("vitamin b-1", ["Vitamin B-12 1000mcg"], None),
        ("vitamin b", ["Vitamin B-12 1000mcg"], None),
        ("vitamin d", ["Vitamin D-3 2000 IU"], None),
        ("vitamin d 3", ["Vitamin D 50mcg"], None),
        ("vitamin d3", ["Vitamin D 50mcg"], None),
        ("omega 6", ["Omega-3"], None),
        ("omega", ["Omega-3"], None),
        ("lisinopril 20", ["Lisinopril 10mg"], None),
        # Joining needs every spoken word to have three letters or more ...
        ("lo vastatin", ["Lovastatin 20mg"], None),
        # ... and none with a digit.
        ("omega 369", ["Omega-3-6-9"], None),
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
