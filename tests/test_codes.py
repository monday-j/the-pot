import pytest

from app.codes import normalise_code, suggest_code
from app.payouts import RuleError


def test_suggest_code_from_name():
    assert suggest_code("Friday Night Brawl") == "FNB"
    assert suggest_code("The Weekend Warriors") == "WWA"
    assert suggest_code("Pixel") == "PIX"
    assert suggest_code("Go") == "GOX"
    assert suggest_code("123") == "SER" or len(suggest_code("123")) == 3


def test_suggest_code_avoids_taken_codes_case_insensitively():
    first = suggest_code("Friday Night Brawl")
    second = suggest_code("Friday Night Brawl", [first.lower()])
    assert second != first and len(second) == 3 and second.isalpha() and second.isupper()
    taken = {second, first}
    assert suggest_code("Friday Night Brawl", taken) not in taken


def test_normalise_code():
    assert normalise_code(" fnb ") == "FNB"
    for bad in ("", "LS", "LSCC", "L5C", "L C"):
        with pytest.raises(RuleError):
            normalise_code(bad)
