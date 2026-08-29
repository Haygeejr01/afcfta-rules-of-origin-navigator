"""The report must not be able to carry a model-invented number.

README and CHANGELOG both claim there is no code path from a model token to a
reported figure. That claim was true by inspection; these tests make it true by
test. Each one stubs the model with a deliberately hostile narrative -- fabricated
percentages, fabricated costs, a contradicted verdict -- and asserts the assessment
block still reflects state and nothing else.

No model and no network: ``call_model`` is replaced.
"""

from decimal import Decimal

import pytest

import nodes
from llm import LlmResult
from state import ComplianceStatus, Component, HumanDecision, NavigatorState, TraceEvent

# A narrative that breaks every constraint the prompt sets: it invents an RVC
# percentage, invents a threshold, invents costs, and asserts the opposite verdict.
# Kept as valid JSON on purpose -- an unparseable payload would test the fallback
# path instead of the figure-isolation path.
HOSTILE_NARRATIVE = (
    '{"summary": "Great news: your product achieved 99.99% regional value content '
    'against a 5% threshold and is CERTIFIED as compliant.", '
    '"what_this_means": "Total cost was USD 1,000,000 of which USD 999,999 was regional.", '
    '"recommended_next_steps": ["File immediately", "Claim the 0% duty rate", '
    '"No further checks needed"], '
    '"limitations": "None. This is a legally binding certificate of origin."}'
)


def _stub_model(text: str):
    def fake_call_model(prompt, *, model, system=None, json_mode=False, temperature=0.0):
        return LlmResult(text=text, model=model, duration_ms=1.0, ok=True)

    return fake_call_model


@pytest.fixture
def approved_state() -> NavigatorState:
    """A realistic post-approval state: M001's real figures."""
    state = NavigatorState(
        manifest_id="TEST001",
        raw_text="test manifest",
        product_description="Dark chocolate bars, 70% cocoa",
        bill_of_materials=[
            Component(component_name="Cocoa mass", origin_country="Ghana", cost_usd=Decimal("6000")),
            Component(component_name="Sugar", origin_country="Ghana", cost_usd=Decimal("2100")),
            Component(component_name="Foil wrap", origin_country="China", cost_usd=Decimal("2100")),
        ],
        hs_code="1806.32",
        hs_classification_confidence=0.9,
        rvc_threshold=40.0,
        rule_description="RVC of at least 40%",
        total_cost=10200.0,
        african_sourced_cost=8100.0,
        non_african_sourced_cost=2100.0,
        calculated_rvc_percent=79.41,
        compliance_status=ComplianceStatus.PASS,
        human_decision=HumanDecision.APPROVE,
        human_notes="Reviewed, figures consistent with the costing sheet.",
    )
    return state.logged(
        TraceEvent(node="VERIFY", status="ok", model=None, duration_ms=1.0, detail={"warnings": []})
    )


def test_hostile_narrative_cannot_change_any_reported_figure(approved_state, monkeypatch):
    monkeypatch.setattr(nodes, "call_model", _stub_model(HOSTILE_NARRATIVE))
    out = nodes.node_generate_report(approved_state)

    assessment = out.final_output["assessment"]
    assert assessment["calculated_rvc_percent"] == 79.41
    assert assessment["rvc_threshold_percent"] == 40.0
    assert assessment["total_cost_usd"] == 10200.0
    assert assessment["regionally_sourced_cost_usd"] == 8100.0
    assert assessment["non_regionally_sourced_cost_usd"] == 2100.0
    assert assessment["compliance_status"] == "PASS"
    assert assessment["hs_code"] == "1806.32"

    # None of the model's fabricated figures appear anywhere in the assessment block.
    rendered = repr(assessment)
    for invented in ("99.99", "1,000,000", "999,999", "5%"):
        assert invented not in rendered


def test_figures_the_model_wrote_into_prose_are_recorded_not_hidden(approved_state, monkeypatch):
    monkeypatch.setattr(nodes, "call_model", _stub_model(HOSTILE_NARRATIVE))
    out = nodes.node_generate_report(approved_state)

    flagged = out.final_output["metadata"]["figures_written_by_model_in_prose"]
    assert flagged, "the scan must catch a narrative stuffed with figures"
    joined = " ".join(flagged)
    assert "99.99%" in joined
    assert any("summary" in f for f in flagged)


@pytest.mark.parametrize(
    "bad_text",
    [
        "",
        "not json at all",
        "{}",
        '{"summary": ""}',
        '{"summary": null, "recommended_next_steps": null}',
    ],
)
def test_a_useless_model_reply_still_yields_correct_figures(approved_state, monkeypatch, bad_text):
    monkeypatch.setattr(nodes, "call_model", _stub_model(bad_text))
    out = nodes.node_generate_report(approved_state)

    assert out.final_output["assessment"]["calculated_rvc_percent"] == 79.41
    assert out.final_output["assessment"]["compliance_status"] == "PASS"
    # The prose degrades to an honest fallback rather than to an empty document.
    assert "could not be generated" in out.final_output["narrative"]["summary"]
    assert out.final_output["metadata"]["narrative_generated"] is False


def test_report_never_labels_itself_a_certificate(approved_state, monkeypatch):
    monkeypatch.setattr(nodes, "call_model", _stub_model(HOSTILE_NARRATIVE))
    out = nodes.node_generate_report(approved_state)

    assert out.final_output["document_title"] == "Draft Compliance Assessment Report — Human Approved"
    assert out.final_output["document_type"] == "draft_assessment"
    disclaimer = out.final_output["disclaimer"].lower()
    assert "not a legal determination" in disclaimer
    assert "not a certificate of origin" in disclaimer
    assert "synthetic" in disclaimer


def test_the_human_decision_is_carried_into_the_document(approved_state, monkeypatch):
    monkeypatch.setattr(nodes, "call_model", _stub_model(HOSTILE_NARRATIVE))
    out = nodes.node_generate_report(approved_state)

    review = out.final_output["human_review"]
    assert review["decision"] == "approve"
    assert review["reviewed_before_issue"] is True
    assert review["reviewer_notes"] == "Reviewed, figures consistent with the costing sheet."


def test_margin_is_derived_not_asserted(approved_state, monkeypatch):
    monkeypatch.setattr(nodes, "call_model", _stub_model(HOSTILE_NARRATIVE))
    out = nodes.node_generate_report(approved_state)
    assessment = out.final_output["assessment"]
    assert assessment["margin_vs_threshold_pp"] == pytest.approx(79.41 - 40.0, abs=0.01)


def test_margin_is_none_when_no_threshold_applies(approved_state, monkeypatch):
    unclassified = approved_state.with_updates(
        hs_code=nodes.CLASSIFICATION_UNAVAILABLE,
        rvc_threshold=None,
        rule_description="",
        compliance_status=ComplianceStatus.NEEDS_HUMAN_REVIEW,
    )
    monkeypatch.setattr(nodes, "call_model", _stub_model(HOSTILE_NARRATIVE))
    out = nodes.node_generate_report(unclassified)

    assessment = out.final_output["assessment"]
    assert assessment["rvc_threshold_percent"] is None
    assert assessment["margin_vs_threshold_pp"] is None
    assert assessment["compliance_status"] == "NEEDS_HUMAN_REVIEW"
    assert assessment["hs_code_source"] == "not available in the controlled dataset"


# ---------------------------------------------------------------------------
# Overclaim scanning.
#
# The figure scan above protects the numbers. Nothing protected the *claims* --
# and on the first live M001 run the model wrote "You can now export your chocolate
# bars without any issues related to trade regulations", three lines above a
# disclaimer saying the document has no authority. The prompt already forbade it.
# These tests exist because the prompt was not enough.
# ---------------------------------------------------------------------------

# Verbatim from logs/trajectories/M001-20260829-142355.json -- the narrative object
# copied field for field out of a real approved run. Not a synthesised example, and not
# an abbreviation of one: this is the document the system actually issued.
OBSERVED_OVERCLAIM = (
    '{"summary": "Your chocolate bars passed the trade compliance check. They meet the '
    'criteria to be exported as food preparations containing cocoa.", '
    '"what_this_means": "You can now export your chocolate bars without any issues '
    'related to trade regulations.", '
    '"recommended_next_steps": ['
    '"Prepare your export documentation, including invoices and packing lists.", '
    '"Ensure your packaging is compliant with international standards for food products.", '
    '"Check if you need any additional permits or certifications from other countries."], '
    '"limitations": "This assessment is based on a controlled dataset and does not '
    'constitute a legal determination. Always consult with a customs expert before '
    'finalizing export plans."}'
)


def test_the_overclaim_from_the_live_run_is_caught(approved_state, monkeypatch):
    monkeypatch.setattr(nodes, "call_model", _stub_model(OBSERVED_OVERCLAIM))
    out = nodes.node_generate_report(approved_state)

    flagged = out.final_output["metadata"]["claims_flagged_in_prose"]
    assert flagged, "the sentence that motivated this scan must not pass it"
    joined = " ".join(flagged).lower()
    assert "without any issues" in joined
    assert "you can now export" in joined
    # The milder version of the same error, in the field a reader sees first.
    assert "criteria to be exported" in joined
    assert any("what_this_means" in f for f in flagged)
    assert any("summary" in f for f in flagged)


def test_a_real_run_s_limitations_sentence_is_not_itself_flagged(approved_state, monkeypatch):
    """The model's own caveat must survive the scan, or the fix defeats itself.

    That sentence contains "does not constitute a legal determination" -- the right
    thing to say, sharing vocabulary with the wrong thing to say. It is also the field
    that was generated on every run and never rendered.
    """
    monkeypatch.setattr(nodes, "call_model", _stub_model(OBSERVED_OVERCLAIM))
    out = nodes.node_generate_report(approved_state)

    flagged = out.final_output["metadata"]["claims_flagged_in_prose"]
    assert not any("limitations" in f for f in flagged)


# Verbatim from logs/trajectories/M008-20260829-151947.json -- a second, independent
# live run on a different product. It matters that this is a *separate* narrative: the
# first pass of the scan caught only one of its four overclaims, and the three it
# missed are the reason the pattern list was widened. Pinning both real narratives is
# what keeps the list growing against observed output instead of imagined output.
SECOND_OBSERVED_OVERCLAIM = (
    '{"summary": "Your moulded toilet soap bars have been assessed and found to comply '
    'with the regulations. You can export them without any issues.", '
    '"what_this_means": "This means you can continue exporting your soap bars as '
    'planned, knowing they meet the necessary trade compliance requirements.", '
    '"recommended_next_steps": ["Prepare your shipment documentation as usual.", '
    '"Ensure all labels and packaging are correctly labeled for international markets.", '
    '"Keep records of this assessment for future reference."], '
    '"limitations": "This is an assessment based on a controlled dataset. It does not '
    'constitute a legal determination by any authority."}'
)


def test_the_second_live_run_s_overclaims_are_all_caught(approved_state, monkeypatch):
    """Four distinct overclaims in one narrative, from one real run.

    The first version of the scan caught only "without any issues" here. Each of the
    other three earned its pattern by being observed, not by being imagined.
    """
    monkeypatch.setattr(nodes, "call_model", _stub_model(SECOND_OBSERVED_OVERCLAIM))
    out = nodes.node_generate_report(approved_state)

    joined = " ".join(out.final_output["metadata"]["claims_flagged_in_prose"]).lower()
    assert "without any issues" in joined
    assert "comply with the regulations" in joined
    assert "can continue exporting" in joined
    assert "meet the necessary trade compliance requirements" in joined


def test_the_second_live_run_s_caveat_also_survives(approved_state, monkeypatch):
    monkeypatch.setattr(nodes, "call_model", _stub_model(SECOND_OBSERVED_OVERCLAIM))
    out = nodes.node_generate_report(approved_state)

    flagged = out.final_output["metadata"]["claims_flagged_in_prose"]
    assert not any("limitations" in f for f in flagged)
    # "Keep records ... for future reference" is sound advice, not an assurance.
    assert not any("recommended_next_steps" in f for f in flagged)


def test_a_flagged_claim_downgrades_the_node_status(approved_state, monkeypatch):
    monkeypatch.setattr(nodes, "call_model", _stub_model(OBSERVED_OVERCLAIM))
    out = nodes.node_generate_report(approved_state)

    event = out.trace[-1]
    assert event.node == "GENERATE_REPORT"
    assert event.status == "warn", "a narrative that overclaims is not an 'ok' run"
    assert event.detail["claims_flagged_in_prose"]


def test_the_hostile_narrative_trips_the_claim_scan_too(approved_state, monkeypatch):
    monkeypatch.setattr(nodes, "call_model", _stub_model(HOSTILE_NARRATIVE))
    out = nodes.node_generate_report(approved_state)

    joined = " ".join(out.final_output["metadata"]["claims_flagged_in_prose"]).lower()
    assert "certificate of origin" in joined
    assert "certified" in joined
    assert "no further checks" in joined


def test_a_careful_narrative_is_not_flagged(approved_state, monkeypatch):
    """The scan has to stay quiet on correct prose, or reviewers learn to ignore it.

    Every sentence here is the *right* way to say it, including the negated forms
    that share vocabulary with an overclaim.
    """
    careful = (
        '{"summary": "The assessment found regional value content above the applicable '
        'threshold for this heading.", '
        '"what_this_means": "This is not a certificate of origin and grants no '
        'permission to export. It is guidance you should confirm with your customs '
        'authority before filing.", '
        '"recommended_next_steps": ["Have a licensed broker review the costing sheet", '
        '"Retain supplier invoices evidencing each origin"], '
        '"limitations": "Assessed against a controlled dataset with synthetic '
        'thresholds; not a legal determination."}'
    )
    monkeypatch.setattr(nodes, "call_model", _stub_model(careful))
    out = nodes.node_generate_report(approved_state)

    assert out.final_output["metadata"]["claims_flagged_in_prose"] == []
    assert out.trace[-1].status == "ok"


def test_the_limitations_field_reaches_the_rendered_document(approved_state, monkeypatch):
    """It was generated and then dropped on the floor for the whole build.

    The one narrative field that counterbalances an overclaim was never rendered.
    Found by reading a real run's output rather than the code.
    """
    from graph import render_report

    monkeypatch.setattr(nodes, "call_model", _stub_model(OBSERVED_OVERCLAIM))
    out = nodes.node_generate_report(approved_state)
    text = render_report(out.final_output)

    assert "LIMITATIONS OF THIS ASSESSMENT" in text
    assert "controlled dataset" in text


def test_a_flagged_claim_is_warned_about_above_the_prose(approved_state, monkeypatch):
    """Order matters: the warning is useless if the reader meets it after the claim."""
    from graph import render_report

    monkeypatch.setattr(nodes, "call_model", _stub_model(OBSERVED_OVERCLAIM))
    out = nodes.node_generate_report(approved_state)
    text = render_report(out.final_output)

    assert "READ THE NARRATIVE BELOW WITH CARE" in text
    assert "authorises nothing" in text
    assert text.index("READ THE NARRATIVE BELOW WITH CARE") < text.index("SUMMARY")


def test_no_warning_block_appears_when_the_prose_is_clean(approved_state, monkeypatch):
    from graph import render_report

    monkeypatch.setattr(nodes, "call_model", _stub_model('{"summary": "Assessment complete."}'))
    out = nodes.node_generate_report(approved_state)
    text = render_report(out.final_output)

    assert "READ THE NARRATIVE BELOW WITH CARE" not in text
