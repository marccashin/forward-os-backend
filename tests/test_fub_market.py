"""A new listing's state: never assumed (Marc, Oct 12, 2026).

Run: python -m pytest tests/test_fub_market.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fub_market import normalise_market, market_from_address, listing_market


def test_fub_names_and_codes():
    for raw, want in [("Maryland", "MD"), ("MD", "MD"), (" maryland ", "MD"), ("Virginia", "VA"),
                      ("Northern Virginia", "VA"), ("NoVA", "VA"), ("va", "VA"), ("DC", "DC"),
                      ("Washington, DC", "DC"), ("Washington DC", "DC"), ("District of Columbia", "DC"),
                      ("D.C.", "DC")]:
        assert normalise_market(raw) == want, raw


def test_fub_unknown_is_blank():
    for raw in ["", None, "Delaware", "West Virginia", "Washington", "Baltimore", "TBD"]:
        assert normalise_market(raw) == "", raw


def test_address_names_the_state():
    assert market_from_address("1300 Army Navy Dr #518, Arlington, VA 22202") == "VA"
    assert market_from_address("1926 Ruxton Rd, Baltimore, MD 21204") == "MD"
    assert market_from_address("601 Pennsylvania Ave NW, Washington, DC 20004") == "DC"
    assert market_from_address("9 Typed Rd, Bethesda, Maryland 20814") == "MD"
    assert market_from_address("100 Alpha Ct, Alexandria Va 22306") == "VA"


def test_address_text_inside_words_decides_nothing():
    # " va" is inside "Valley"; the old fallback created this Maryland house as VA
    assert market_from_address("300 Valley Rd, Bethesda, MD 20817") == "MD"
    assert market_from_address("300 Valley Rd") == ""
    assert market_from_address("12 Vanderbilt Ct") == ""
    assert market_from_address("8 Mdina Way") == ""


def test_no_state_named_is_blank_never_dc():
    for a in ["9124 Potomac Ridge Rd", "1300 Army Navy Dr #518", "Kylie & Brian Dowling", "", None,
              "4101 Albemarle Street NW #612", "5027 11th st NE, 20017"]:
        assert market_from_address(a) == "", a


def test_a_city_alone_decides_nothing():
    for a in ["12 Main St, Arlington", "4 Oak Ln, Bethesda", "7 Elm St, Fairfax"]:
        assert market_from_address(a) == "", a


def test_two_states_named_decides_nothing():
    assert market_from_address("1 Line Rd, near the MD and VA line") == ""


def test_listing_market_order():
    assert listing_market("Virginia", "1 Test St, Rockville, MD 20850") == "VA"   # FUB's Market wins
    assert listing_market("", "1 Test St, Rockville, MD 20850") == "MD"
    assert listing_market("", "9124 Potomac Ridge Rd") == ""                       # was "DC"
    assert listing_market("Delaware", "Kylie & Brian Dowling") == ""
