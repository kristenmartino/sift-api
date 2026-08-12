"""The EIN-name agreement check, and the parser it guards.

Every case in TestRealFilingCases came out of the first ten-org pull
(2026-08-11) — they are regressions, not hypotheticals.
"""

from __future__ import annotations

from services.funding_edges import (
    FundingEdge,
    NameVerdict,
    apply_verdicts,
    ein_name_agrees,
    names_agree,
    normalize_org_name,
    parse_filing,
)

# EIN -> official IRS name, as the annual index reports it.
INDEX = {
    "520880375": {"THE URBAN INSTITUTE"},
    "042103580": {"President and Fellows of Harvard College"},
    "953443202": {
        "THE CLAREMONT INSTITUTE FOR THE STUDY OF STATESMANSHIP & POLITICAL PHILOSOPHY"
    },
    "272244700": {"HERITAGE ACTION FOR AMERICA"},
    "530196549": {"AMERICAN UNIVERSITY"},
}


class TestNormalize:
    def test_strips_legal_suffixes_and_punctuation(self):
        assert normalize_org_name("Gallup, Inc.") == "GALLUP"
        assert normalize_org_name("The Urban Institute") == "URBAN INSTITUTE"

    def test_keeps_meaningful_words(self):
        # "Institute" and "Foundation" carry identity; dropping them is what
        # made an earlier version of this check certify a false match.
        assert "INSTITUTE" in normalize_org_name("The Urban Institute")
        assert "FOUNDATION" in normalize_org_name("Heritage Foundation")

    def test_handles_none_and_empty(self):
        assert normalize_org_name(None) == ""
        assert normalize_org_name("   ") == ""


class TestNamesAgree:
    def test_identical(self):
        assert names_agree("American University", "AMERICAN UNIVERSITY")

    def test_legal_suffix_only_difference(self):
        assert names_agree("Gallup Inc", "GALLUP")

    def test_short_form_is_accepted_via_containment(self):
        assert names_agree(
            "CLAREMONT INSTITUTE",
            "THE CLAREMONT INSTITUTE FOR THE STUDY OF STATESMANSHIP & POLITICAL PHILOSOPHY",
        )

    def test_single_shared_word_is_not_enough(self):
        # The defect this whole module exists for.
        assert not names_agree("URBAN LEAGUE OF LOUISIANA", "THE URBAN INSTITUTE")

    def test_one_token_containment_rejected(self):
        assert not names_agree("HARVARD", "HARVARD LAW SCHOOL SOMETHING ELSE")

    def test_unrelated_orgs(self):
        assert not names_agree("Cato Institute", "Brookings Institution")

    def test_abbreviations_and_word_order_agree(self):
        # All observed in the real pull; all the same organization spelled
        # differently by the filer. A 0.85 similarity floor held these back.
        assert names_agree(
            "AMERICAN ASSOC OF PRO-LIFE OBSTETRICIANS & GYNECOLOGISTS",
            "ASSOCIATION OF PRO-LIFE OBSTETRICIANS AND GYNECOLOGISTS",
        )
        assert names_agree("FEDS FOR FREEDOM", "FEDS 4 MED FREEDOM INC")
        assert names_agree("VA COALITION ON IMMIGRANT RIGHTS",
                           "Virginia Coalition For Immigrant Rights")
        assert names_agree("NEW VIRGINIA MAJORITY EDUCATION FUND",
                           "VIRGINIA NEW MAJORITY EDUCATION FUND")

    def test_dba_and_longer_forms_agree_via_containment(self):
        assert names_agree("PREVENT CHILD ABUSE VIRGINIA DBA FAMILIES FORWARD",
                           "PREVENT CHILD ABUSE VIRGINIA")
        assert names_agree("WASHINGTON UNIVERSITY IN ST LOUIS", "WASHINGTON UNIVERSITY")
        assert names_agree("BARRED BUSINESS", "Barred Business Foundation")

    def test_the_calibration_gap_holds(self):
        # The floor sits between the highest real defect (0.35-0.44) and the
        # lowest legitimate variant (0.65). Both sides must stay on their side.
        assert not names_agree("URBAN LEAGUE OF LOUISIANA", "THE URBAN INSTITUTE")
        assert names_agree("PREVENT CHILD ABUSE VIRGINIA DBA FAMILIES FORWARD",
                           "PREVENT CHILD ABUSE VIRGINIA")

    def test_empty_never_agrees(self):
        assert not names_agree(None, "THE URBAN INSTITUTE")
        assert not names_agree("THE URBAN INSTITUTE", None)


class TestRealFilingCases:
    """Cases observed in the 2026-08-11 pull of ten think-tank 990s."""

    def test_wrong_ein_is_held_for_review(self):
        # Brookings filed Urban League of Louisiana under Urban Institute's EIN.
        verdict, official = ein_name_agrees(
            "URBAN LEAGUE OF LOUISIANA", "520880375", INDEX
        )
        assert verdict is NameVerdict.REVIEW
        assert official == "THE URBAN INSTITUTE"

    def test_sub_unit_is_held_for_review_not_asserted_wrong(self):
        # Legitimate (Harvard Law is inside the College's entity) but no string
        # comparison can know that — so it is held, not rejected outright.
        verdict, _ = ein_name_agrees("HARVARD LAW SCHOOL", "042103580", INDEX)
        assert verdict is NameVerdict.REVIEW

    def test_short_form_of_a_long_official_name_agrees(self):
        verdict, _ = ein_name_agrees("CLAREMONT INSTITUTE", "953443202", INDEX)
        assert verdict is NameVerdict.AGREES

    def test_exact_match_agrees(self):
        verdict, _ = ein_name_agrees("HERITAGE ACTION FOR AMERICA", "272244700", INDEX)
        assert verdict is NameVerdict.AGREES

    def test_ein_absent_from_index(self):
        # Consultancies and LLCs (Cities GPS LLC etc.) do not e-file a 990.
        verdict, official = ein_name_agrees("CITIES GPS LLC", "177607014", INDEX)
        assert verdict is NameVerdict.EIN_ABSENT
        assert official is None

    def test_malformed_ein_is_reviewed_not_trusted(self):
        assert ein_name_agrees("SOMEONE", "12345", INDEX)[0] is NameVerdict.REVIEW
        assert ein_name_agrees("SOMEONE", None, INDEX)[0] is NameVerdict.REVIEW


SCHEDULE_I_XML = """
<Return xmlns="http://www.irs.gov/efile">
  <ReturnData>
    <IRS990ScheduleI>
      <RecipientTable>
        <RecipientBusinessName><BusinessNameLine1Txt>THE URBAN INSTITUTE</BusinessNameLine1Txt></RecipientBusinessName>
        <RecipientEIN>520880375</RecipientEIN>
        <IRCSectionDesc>501(C)(3)</IRCSectionDesc>
        <CashGrantAmt>113213</CashGrantAmt>
        <PurposeOfGrantTxt>RESEARCH COLLABORATION</PurposeOfGrantTxt>
      </RecipientTable>
    </IRS990ScheduleI>
    <IRS990ScheduleR>
      <IdRelatedTaxExemptOrgGrp>
        <DisregardedEntityName><BusinessNameLine1Txt>HERITAGE ACTION FOR AMERICA</BusinessNameLine1Txt></DisregardedEntityName>
        <EIN>272244700</EIN>
        <PrimaryActivitiesTxt>ADVOCACY</PrimaryActivitiesTxt>
        <ExemptCodeSectionTxt>501(C)(4)</ExemptCodeSectionTxt>
      </IdRelatedTaxExemptOrgGrp>
    </IRS990ScheduleR>
  </ReturnData>
</Return>
"""


def _parse():
    return parse_filing(
        SCHEDULE_I_XML,
        source_ein="237327730",
        source_name="Heritage Foundation",
        fiscal_period="202412",
        object_id="202523199349302027",
        filing_url="https://example.test/filing",
    )


class TestParseFiling:
    def test_extracts_both_schedules(self):
        edges = _parse()
        assert {e.edge_kind for e in edges} == {"grant", "related_org"}

    def test_grant_fields(self):
        grant = next(e for e in _parse() if e.edge_kind == "grant")
        assert grant.target_ein == "520880375"
        assert grant.amount_usd == 113213
        assert grant.purpose == "RESEARCH COLLABORATION"
        assert grant.form == "990 Sch I Part II"

    def test_related_org_name_survives_the_reused_element_tag(self):
        # Regression: the schema stores the related org's name in a
        # <DisregardedEntityName> element. Reading only
        # <RelatedOrganizationName> silently yields None for every row, which
        # is indistinguishable from "this filer declared no related orgs".
        rel = next(e for e in _parse() if e.edge_kind == "related_org")
        assert rel.target_name_as_filed == "HERITAGE ACTION FOR AMERICA"
        assert rel.target_ein == "272244700"
        assert rel.exempt_code == "501(C)(4)"

    def test_namespaced_xml_is_handled(self):
        # Real IRS filings carry the efile namespace on every element.
        assert len(_parse()) == 2


class TestApplyVerdicts:
    def test_every_edge_gets_a_verdict(self):
        edges = apply_verdicts(_parse(), INDEX)
        assert all(isinstance(e.verdict, NameVerdict) for e in edges)

    def test_agreeing_edge_records_the_irs_name(self):
        edges = apply_verdicts(_parse(), INDEX)
        grant = next(e for e in edges if e.edge_kind == "grant")
        assert grant.verdict is NameVerdict.AGREES
        assert grant.target_name_irs == "THE URBAN INSTITUTE"

    def test_does_not_mutate_the_filed_name(self):
        # The verbatim string from the filing is evidence; never normalize it
        # away in favour of the IRS's spelling.
        edges = apply_verdicts(
            [
                FundingEdge(
                    source_ein="530196577",
                    source_name="Brookings Institution",
                    target_ein="520880375",
                    target_name_as_filed="URBAN LEAGUE OF LOUISIANA",
                    edge_kind="grant",
                    amount_usd=43334,
                    purpose="RESEARCH COLLABORATION",
                    exempt_code="501(C)(3)",
                    fiscal_period="202506",
                    form="990 Sch I Part II",
                    object_id="202601359349300310",
                    filing_url="https://example.test/f",
                )
            ],
            INDEX,
        )
        assert edges[0].target_name_as_filed == "URBAN LEAGUE OF LOUISIANA"
        assert edges[0].target_name_irs == "THE URBAN INSTITUTE"
        assert edges[0].verdict is NameVerdict.REVIEW
