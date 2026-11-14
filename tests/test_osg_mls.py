"""Offer Strategy readers: the facts added Nov 4 2026.

Marc's 3 Old Sound Rd run (Joppa, MD): the strategy said the sheet printed
no water access and priced the home off non-waterfront comps, while the
sheet printed Waterfront: Yes, 30 ft of frontage, a deeded slip, 500 sf of
finished below-grade space and a long public description. None of it was
read. These tests pin what is read now and what is refused.

The sheet text below copies Bright's Agent Full LABELS and layout (as pypdf
extracts them), with made-up properties, so no real listing agent's details
sit in this public repo.
"""

import osg_mls as o

SUBJECT = """Agent Full
9 Test Cove Rd, Joppa, MD 21085 Active Residential
  $630,000
MLS #: MDTEST0000001
Ownership Interest: Fee Simple
Structure Type: Detached
Waterfront: Yes
Views: Canal, Water
Beds: 3
Baths: 2 / 1
Above Grade Fin SQFT: 1,836
Assessor AbvGrd Fin SQFT: 1,236
Year Built: 1975
Basement: Yes
Waterfront / Water Access
Waterfront Features: Boat/Launch Ramp - Private
Water Access: Yes; Boat - Powered, Canoe/Kayak,
Swimming Allowed
Navigable Water: Yes
Tidal Water: Yes
Dock Type: 1  Material - Treated Lumber
Dock Slip Conveyance: Deeded
Water Frontage in FT: 30
Riparian Rights: Yes
Building Info
Yr Major Reno/Remodel: 2021
Above Grade Fin SQFT: 1,836
Below Grade Fin SQFT: 500
Total Below Grade SQFT: 500 / Assessor
Total Fin SQFT: 2,336 / Assessor
Tax Total Fin SQFT: 1,736
Basement Type: Connecting Stairway, Fully Finished,
Heated, Walkout Level
Remarks
Agent: Seller asks that all inspections be for information only. Call 410-555-0100 with questions.
Public: Welcome to a beautifully updated WATERFRONT home. The pier was replaced in 2025 and has water and
electric. Alarm code is 1234 for showings. Rear yard hardscaping was completed in 2025.
Listing Office
Listing Office: Test Realty
"""

PUBLIC = ("Welcome to a beautifully updated WATERFRONT home. The pier was replaced in 2025 and has "
          "water and electric. Alarm code is 1234 for showings. Rear yard hardscaping was completed in 2025.")

GOOD = {
    "address": "9 Test Cove Rd", "mlsNumber": "MDTEST0000001",
    "gla": "1836", "assessorGla": "1236", "belowGradeFin": "500", "totalFinSqft": "2336",
    "renoYear": "2021", "waterfront": "Yes", "views": "Canal, Water",
    "waterfrontFeatures": "Boat/Launch Ramp - Private",
    "waterAccess": "Yes; Boat - Powered, Canoe/Kayak, Swimming Allowed",
    "navigableWater": "Yes", "tidalWater": "Yes", "waterFrontage": "30",
    "dockType": "1 Material - Treated Lumber", "dockSlip": "Deeded", "riparianRights": "Yes",
    "basement": "Connecting Stairway, Fully Finished, Heated, Walkout Level",
    "publicRemarks": PUBLIC,
}

NEW_KEYS = ("assessorGla", "belowGradeFin", "totalFinSqft", "renoYear", "waterfront", "views",
            "waterfrontFeatures", "waterAccess", "navigableWater", "tidalWater", "waterFrontage",
            "dockType", "dockSlip", "riparianRights")


def test_subject_keeps_every_new_fact_the_sheet_prints():
    r = o.verify_subject(dict(GOOD), SUBJECT)
    s = r["subject"]
    for k in NEW_KEYS:
        assert s[k], k
    assert s["belowGradeFin"] == "500" and s["totalFinSqft"] == "2336"
    assert s["assessorGla"] == "1236" and s["waterFrontage"] == "30"
    assert "Fully Finished" in s["basement"]
    assert r["unverified"] == []


def test_subject_refuses_values_from_the_wrong_label():
    bad = dict(GOOD,
               totalFinSqft="1736",    # Tax Total Fin SQFT, not the finished area
               belowGradeFin="2336",   # the total, not the below-grade part
               assessorGla="1836",     # the listing's figure, not the assessor's
               waterfront="No", waterFrontage="62", renoYear="1975", dockSlip="Assigned")
    r = o.verify_subject(bad, SUBJECT)
    for k in ("totalFinSqft", "belowGradeFin", "assessorGla", "waterfront",
              "waterFrontage", "renoYear", "dockSlip"):
        assert r["subject"][k] == "", k
        assert k in r["unverified"], k


def test_public_remarks_kept_minus_sensitive_sentences():
    r = o.verify_subject(dict(GOOD), SUBJECT)
    rem = r["subject"]["publicRemarks"]
    assert "pier was replaced in 2025" in rem
    assert "hardscaping was completed in 2025" in rem
    assert "Alarm" not in rem and "1234" not in rem
    assert r["dropped_remark_sentences"] == 1


def test_agent_remarks_never_pass_as_public():
    agent = "Seller asks that all inspections be for information only."
    assert o.verify_public_remarks(agent, SUBJECT) == ("", 1)
    r = o.verify_subject(dict(GOOD, publicRemarks=agent), SUBJECT)
    assert r["subject"]["publicRemarks"] == ""
    assert "publicRemarks" in r["unverified"]


def test_an_edited_paragraph_keeps_the_sentences_still_word_for_word():
    edited = PUBLIC.replace("WATERFRONT home", "waterfront property")
    rem, dropped = o.verify_public_remarks(edited, SUBJECT)
    assert "waterfront property" not in rem
    assert "pier was replaced in 2025" in rem
    assert dropped == 2   # the edited sentence and the alarm sentence


def test_no_public_remarks_is_not_an_error():
    r = o.verify_subject(dict(GOOD, publicRemarks=""), SUBJECT)
    assert r["subject"]["publicRemarks"] == ""
    assert "publicRemarks" not in r["unverified"]


def test_fields_from_before_nov_4_are_unchanged():
    """The older fields verify exactly as before: same value, same refusals."""
    r = o.verify_subject(dict(GOOD, gla="2336", yearBuilt="1975"), SUBJECT)
    assert r["subject"]["gla"] == "" and "gla" in r["unverified"]
    assert r["subject"]["yearBuilt"] == "1975"


COMPS = """Agent Full
1 Test Water Dr, Joppa, MD 21085 Closed | 05/26/26 Residential $780,000
MLS #: MDTEST0000010
Waterfront: Yes
Views: Trees/Woods, Water
Basement: No
Water Frontage in FT: 62
Dock Slip Conveyance: Deeded
Yr Major Reno/Remodel: 2016
Total Fin SQFT: 2,534 / Assessor
Tax Total Fin SQFT: 2,114
2 Test Dry Rd, Joppa, MD 21085 Closed | 12/04/25 Residential $415,000
MLS #: MDTEST0000020
Waterfront: No
Basement: Yes
Total Fin SQFT: 1,824 / Assessor
Basement Type: Connecting Stairway, Daylight, Full,
Fully Finished, Walkout Level
"""

WET = dict(mlsNumber="MDTEST0000010", waterfront="Yes", waterFrontage="62", views="Trees/Woods, Water",
           dockSlip="Deeded", basement="No", totalFin="2534", renoYear="2016")
DRY = dict(mlsNumber="MDTEST0000020", waterfront="No", waterFrontage="", views="", dockSlip="",
           basement="Connecting Stairway, Daylight, Full, Fully Finished, Walkout Level",
           totalFin="1824", renoYear="")


def test_comp_extras_kept_from_the_comps_own_block():
    for src in (WET, DRY):
        it = dict(src)
        assert o.verify_comp_extras(it, COMPS) == []
        for k in o.COMP_EXTRAS:
            if k in src:
                assert it[k] == src[k], k


def test_comp_extras_from_another_property_are_blanked():
    # The waterfront facts of comp 1 claimed for comp 2 must not survive,
    # although "Waterfront: Yes" and "62" are printed elsewhere in the file.
    it = dict(WET, mlsNumber="MDTEST0000020")
    blanked = o.verify_comp_extras(it, COMPS)
    assert {"waterfront", "waterFrontage", "views", "dockSlip", "totalFin", "renoYear"} <= set(blanked)
    assert it["waterfront"] == "" and it["unverified"] == blanked


def test_comp_tax_total_is_not_total_finished():
    it = dict(WET, totalFin="2114")
    assert "totalFin" in o.verify_comp_extras(it, COMPS)


def test_comp_without_a_findable_mls_number_gets_no_extras():
    given = {k for k in o.COMP_EXTRAS if WET.get(k)}
    it = dict(WET, mlsNumber="")
    assert set(o.verify_comp_extras(it, COMPS)) == given
    it = dict(WET, mlsNumber="MDTEST000001")   # a prefix of a real number
    assert set(o.verify_comp_extras(it, COMPS)) == given


def test_prompts_ask_for_the_new_fields():
    for k in NEW_KEYS + ("publicRemarks",):
        assert '"' + k + '"' in o.SUBJECT_PARSE_PROMPT, k
    for k in o.COMP_EXTRAS:
        assert '"' + k + '"' in o.OSG_COMP_PARSE_PROMPT, k


# Live run, Nov 4 2026 (second pass). Production's pypdf prints the MLS
# number straight into the next label ("MLS #:MDHR2054110Tax ID #:"), which
# the first version refused, blanking every added comp field on Marc's real
# sheet. And Claude returned 2 Old Sound Rd's "Below Grade Unfin SQFT: 576" as
# finished space. Layout below copies production's text, made-up values.
JOINED = (
    "1 Test Water Dr, Joppa, MD 21085 Closed | 05/26/26 Residential $780,000\n"
    "MLS #:MDTEST0000010Tax ID #:1301000001Beds:3Baths:2 / 1Year Built:1968Garage:Yes"
    "Waterfront:YesViews:Trees/Woods, WaterWater Frontage in FT:62"
    "Dock Slip Conveyance:DeededLot Acres / SQFT:0.37a / 16097sf / Estimated"
    "Assessor AbvGrd Fin SQFT:2,114Total Fin SQFT:2,534 / AssessorTax Total Fin SQFT:2,114"
    "ParkingAttached Garage - # of Spaces 2Total Parking Spaces 6Basement:No\n"
    "2 Test Dry Rd, Joppa, MD 21085 Closed | 12/04/25 Residential $415,000\n"
    "MLS #:MDTEST0000020Tax ID #:1301000002Beds:3Baths:2 / 1Year Built:1976Garage:No"
    "Waterfront:NoAbove Grade Fin SQFT:1,824 / AssessorTotal Below Grade SQFT:576 / Assessor"
    "Total Fin SQFT:1,824 / AssessorBasement Type:Connecting Stairway, Daylight, Full,"
    "Fully Finished, Walkout LevelBelow Grade Unfin SQFT:576 / AssessorLot Acres / SQFT:0.25a / 10881sf / Assessor\n"
)


def test_mls_number_joined_to_the_next_label_is_found():
    it = dict(mlsNumber="MDTEST0000010", waterfront="Yes", waterFrontage="62", dockSlip="Deeded",
              totalFin="2534", basement="No", beds="3", fullBaths="2", halfBaths="1",
              yearBuilt="1968", assessorGla="2114", lotSize="16097", garageSpaces=2)
    assert o.verify_comp_extras(it, JOINED) == []
    assert it["waterfront"] == "Yes" and it["garageSpaces"] == "2" and it["lotSize"] == "16097"


def test_a_shorter_mls_number_still_finds_nothing():
    it = dict(mlsNumber="MDTEST000001", waterfront="Yes")   # prefix of MDTEST0000010
    assert o.verify_comp_extras(it, JOINED) == ["waterfront"]


def test_unfinished_below_grade_is_never_taken_as_finished():
    it = dict(mlsNumber="MDTEST0000020", below="576", waterfront="No")
    assert o.verify_comp_extras(it, JOINED) == ["below"]
    assert it["below"] == "" and it["waterfront"] == "No"


def test_garage_zero_kept_only_when_the_sheet_says_no_garage():
    it = dict(mlsNumber="MDTEST0000020", garageSpaces=0)
    assert o.verify_comp_extras(it, JOINED) == [] and it["garageSpaces"] == "0"
    it = dict(mlsNumber="MDTEST0000010", garageSpaces=0)   # that one has a garage
    assert o.verify_comp_extras(it, JOINED) == ["garageSpaces"]
    it = dict(mlsNumber="MDTEST0000010", garageSpaces=6)   # total parking, not garage
    assert o.verify_comp_extras(it, JOINED) == ["garageSpaces"]


def test_beds_baths_year_and_lot_from_another_comp_are_blanked():
    it = dict(mlsNumber="MDTEST0000020", beds="4", fullBaths="1", halfBaths="0",
              yearBuilt="1968", lotSize="16097", assessorGla="2114")
    assert set(o.verify_comp_extras(it, JOINED)) == {"beds", "fullBaths", "halfBaths",
                                                      "yearBuilt", "lotSize", "assessorGla"}
