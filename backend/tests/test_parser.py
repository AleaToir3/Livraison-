import json
from pathlib import Path

import pytest

from uk_address_parser import (
    POSTCODE_RE,
    make_cache_key,
    normalize_building_token,
    normalize_postcode,
    parse_structured,
    parse_uk_address,
)

FIXTURES = json.loads(
    (Path(__file__).resolve().parents[2] / "shared" / "address_fixtures.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", FIXTURES, ids=[c["name"] for c in FIXTURES])
def test_fixture(case):
    parsed = parse_uk_address(case["text"])
    expected = case["expected"]
    assert parsed.postcode == expected["postcode"]
    assert parsed.building_number == expected["building_number"]
    assert parsed.sub_building == expected["sub_building"]
    assert parsed.building_name == expected["building_name"]
    assert parsed.street == expected["street"]
    assert parsed.cache_key == expected["cache_key"]
    assert parsed.is_fast_path_valid == expected["valid"]


def test_official_regex_is_the_one_from_the_spec():
    assert POSTCODE_RE.pattern == r"\b([A-Z]{1,2}[0-9][A-Z0-9]?)\s*([0-9][A-Z]{2})\b"


@pytest.mark.parametrize(
    "raw,expected",
    [("ls61ab", "LS6 1AB"), ("LS6  1AB", "LS6 1AB"), ("sw1a2aa", "SW1A 2AA"), ("ZZ9 9ZZ", None), ("", None), (None, None), ("12345", None)],
)
def test_normalize_postcode(raw, expected):
    assert normalize_postcode(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("14 a", "14A"), ("12 - 14", "12-14"), ("no. 9", "9"), ("the  laurels", "THE LAURELS"), ("", None), (None, None)],
)
def test_normalize_building_token(raw, expected):
    assert normalize_building_token(raw) == expected


def test_cache_key_is_building_level_and_stable():
    a = parse_uk_address("FLAT 1, 27 ALBION AVENUE\nLEEDS LS6 1AD")
    b = parse_uk_address("FLAT 2, 27 ALBION AVENUE\nLEEDS LS6 1AD")
    assert a.cache_key == b.cache_key == "LS61AD:27"
    assert make_cache_key("LS6 1AB", "ROSE COTTAGE") == "LS61AB:ROSE-COTTAGE"


def test_parse_structured_number_and_name():
    n = parse_structured("ls6 1ab", "14a", street="Albion Avenue")
    assert (n.postcode, n.building_number, n.building_name, n.street) == ("LS6 1AB", "14A", None, "ALBION AVENUE")
    h = parse_structured("LS6 1AB", "Rose Cottage")
    assert (h.building_number, h.building_name) == (None, "ROSE COTTAGE")
    full = parse_structured("LS6 1AB", "14 Albion Avenue")
    assert (full.building_number, full.street) == ("14", "ALBION AVENUE")
    assert parse_structured("nope", "14").is_fast_path_valid is False
