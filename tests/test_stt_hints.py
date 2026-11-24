"""Speech-to-text's vocabulary hints: built once at start-up from the medications table, the
doctors and the clinic's name; letters-only words, bounded, linked to no patient; and the doctors
and the clinic alone when the database cannot be read."""

import logging
import re
from datetime import date

import pytest
from conftest import offline_settings
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.seed import load_seed_data, seed
from app.db.session import create_session_factory
from app.main import create_app
from app.voice.vocabulary import Vocabulary, keyterms

SEED_DAY = date(2026, 11, 24)
# A word or a few, letters only, 2 to 40 characters.
VOCABULARY_TERM = re.compile(r"[A-Za-z]+( [A-Za-z]+)*")


@pytest.mark.anyio
async def test_hints_are_vocabulary_only(owner_session: AsyncSession, runtime: AsyncEngine) -> None:
    raw = load_seed_data()
    await seed(owner_session, raw, SEED_DAY)
    await owner_session.commit()
    vocabulary = Vocabulary()

    # Read as the application reads it: the runtime role.
    await vocabulary.load(create_session_factory(runtime))

    # The seed's medications by their drug word, the doctors' surnames and the clinic.
    assert vocabulary.terms == (
        "Albuterol",
        "Atorvastatin",
        "Lee",
        "Levothyroxine",
        "Lisinopril",
        "Metformin",
        "Patel",
        "Sunrise Family Clinic",
    )
    for patient in raw["patients"]:
        for value in (patient["first_name"], patient["last_name"], patient["id"]):
            assert all(value not in term for term in vocabulary.terms), value

    # Whatever the table holds: only letters-only words of 2 to 40 characters, never a dose,
    # a number or a date, at most 100, sorted, each once; the doctors and the clinic always.
    many = [f"Drug{'x' * (n % 3)}{chr(97 + n % 26)}{chr(97 + n // 26)} 5mg" for n in range(200)]
    names = [
        *many,
        "Lisinopril 10mg",
        "Lisinopril 20mg",
        "Zz",
        "Q",
        "Vitamin-D3 1000IU",
        "B12 injection",
        "1985-03-14",
        "4421 tablets",
        "Sarah's cream",
        "  ",
        "",
        "A" * 41,
        "Hydrochlorothiazide 25mg",
    ]
    terms = keyterms(names)
    assert len(terms) == 100
    assert list(terms) == sorted(set(terms))
    assert {"Lee", "Patel", "Sunrise Family Clinic"} <= set(terms)
    for term in terms:
        assert VOCABULARY_TERM.fullmatch(term), term
        assert 2 <= len(term) <= 40, term
    assert keyterms(["Lisinopril 10mg", "Lisinopril 20mg", "Zz", "Q", "Vitamin-D3 1000IU"]) == (
        "Lee",
        "Lisinopril",
        "Patel",
        "Sunrise Family Clinic",
        "Zz",
    )


def test_hints_fall_back(caplog: pytest.LogCaptureFixture) -> None:
    # The offline settings name a database host that does not exist.
    caplog.set_level(logging.INFO)
    app = create_app(offline_settings())

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        vocabulary: Vocabulary = app.state.vocabulary
        assert vocabulary.terms == ("Lee", "Patel", "Sunrise Family Clinic")

    logged = [r.getMessage() for r in caplog.records if r.getMessage().startswith("stt_hints")]
    assert len(logged) == 1
    assert re.fullmatch(r"stt_hints_fallback error=[A-Za-z]+", logged[0]), logged
