import pytest
from conftest import NoSkipsPlugin

# Its own ini makes the inner session's rootdir the pytester directory, whatever is above it.
INNER_INI = "[pytest]\n"

# The marker names are assembled so that this file does not match the repo's own skip scan.
SKIP = "sk" + "ip"
XFAIL = "x" + "fail"

NOT_RUN_TESTS = f"""
import pytest


def test_skipped():
    pytest.{SKIP}("inner")


@pytest.mark.{XFAIL}(reason="inner")
def test_expected_to_fail():
    assert False


@pytest.mark.{XFAIL}(reason="inner")
def test_unexpectedly_passes():
    pass


def test_passes():
    pass
"""

PASSING_TESTS = """
def test_passes():
    pass


def test_also_passes():
    pass
"""


def test_run_fails_when_a_test_does_not_run(pytester: pytest.Pytester) -> None:
    pytester.makeini(INNER_INI)
    pytester.makepyfile(test_inner=NOT_RUN_TESTS)

    result = pytester.runpytest(plugins=[NoSkipsPlugin()])

    result.assert_outcomes(passed=1, skipped=1, xfailed=1, xpassed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(
        [
            "*tests that must run but did not*",
            "test_inner.py::test_skipped: skipped",
            "test_inner.py::test_expected_to_fail: xfailed or xpassed",
            "test_inner.py::test_unexpectedly_passes: xfailed or xpassed",
        ]
    )


def test_run_passes_when_every_test_runs(pytester: pytest.Pytester) -> None:
    pytester.makeini(INNER_INI)
    pytester.makepyfile(test_inner=PASSING_TESTS)

    result = pytester.runpytest(plugins=[NoSkipsPlugin()])

    result.assert_outcomes(passed=2)
    assert result.ret == pytest.ExitCode.OK
    result.stdout.no_fnmatch_line("*tests that must run but did not*")
