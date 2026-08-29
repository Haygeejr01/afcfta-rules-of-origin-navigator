"""The human checkpoint is the hard requirement. These tests hold it to that.

The whole workflow runs here with the model stubbed out, so the control flow can be
asserted without inference: no report without an approve, a reject producing no
document at all, and the reviewer's edit loop actually recalculating and staying
bounded.

The stub dispatches on prompt content, which means these tests break if a prompt's
shape changes -- that is intentional. A prompt that no longer asks for the shape the
nodes parse is a defect worth failing on.
"""

import pytest

import graph
import nodes
from llm import LlmResult
from state import ComplianceStatus, HumanDecision

MANIFEST = """Chocolate bars, 70% cocoa.
Cocoa mass, Ghana, 6000
Sugar, Ghana, 2100
Foil wrap, China, 2100
"""

EXTRACTED = """{
  "product_description": "Dark chocolate bars, 70% cocoa",
  "components": [
    {"component_name": "Cocoa mass", "origin_country": "Ghana", "cost_usd": 6000},
    {"component_name": "Sugar", "origin_country": "Ghana", "cost_usd": 2100},
    {"component_name": "Foil wrap", "origin_country": "China", "cost_usd": 2100}
  ]
}"""

NARRATIVE = """{
  "summary": "The assessment is complete.",
  "what_this_means": "See the figures above.",
  "recommended_next_steps": ["Confirm with your customs authority"],
  "limitations": "Assessment against a controlled dataset, not a legal determination."
}"""


def _stub(hs_code: str = "1806.32"):
    """Dispatch canned replies by prompt shape. Records what was asked."""
    calls: list[str] = []

    def fake_call_model(prompt, *, model, system=None, json_mode=False, temperature=0.0):
        if "--- MANIFEST ---" in prompt:
            calls.append("EXTRACT")
            text = EXTRACTED
        elif "Choose the single best-fitting HS code" in prompt:
            calls.append("CLASSIFY_HS")
            text = f'{{"hs_code": "{hs_code}", "confidence": 0.9, "rationale": "cocoa product"}}'
        elif "narrative sections" in prompt:
            calls.append("GENERATE_REPORT")
            text = NARRATIVE
        else:  # pragma: no cover - a new prompt shape should be loud, not silent
            raise AssertionError(f"unrecognised prompt reached the model:\n{prompt[:200]}")
        return LlmResult(text=text, model=model, duration_ms=1.0, ok=True)

    return fake_call_model, calls


def _provider(*decisions):
    """A scripted reviewer that answers each visit to the checkpoint in turn."""
    script = list(decisions)

    def provider(state):
        decision, edited = script.pop(0)
        return HumanDecision(decision), f"scripted: {decision}", edited

    return provider


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    stub, calls = _stub()
    monkeypatch.setattr(nodes, "call_model", stub)
    return calls


def _run(*decisions, monkeypatch=None, hs_code="1806.32"):
    if monkeypatch is not None:
        stub, calls = _stub(hs_code)
        monkeypatch.setattr(nodes, "call_model", stub)
    else:
        calls = None
    state = graph.run_assessment(
        MANIFEST,
        manifest_id="TEST",
        decision_provider=_provider(*decisions),
        show_panel=False,
        verbose=False,
    )
    return state, calls


def test_approve_is_the_only_path_to_a_document(monkeypatch):
    state, calls = _run(("approve", None), monkeypatch=monkeypatch)
    assert state.human_decision is HumanDecision.APPROVE
    assert state.final_output is not None
    assert "GENERATE_REPORT" in calls


@pytest.mark.parametrize("decision", ["reject", "edit"])
def test_no_document_without_an_approve(monkeypatch, decision):
    # 'edit' with no code behaves as a terminal decision; both must leave no report.
    state, calls = _run((decision, None), monkeypatch=monkeypatch)
    assert state.final_output is None, "a document was produced without an approval"
    assert "GENERATE_REPORT" not in calls, "the report node ran without an approval"


def test_reject_records_the_decision_rather_than_discarding_the_work(monkeypatch):
    state, _ = _run(("reject", None), monkeypatch=monkeypatch)
    assert state.human_decision is HumanDecision.REJECT
    # The assessment still happened and is still inspectable; only the document is withheld.
    assert state.calculated_rvc_percent == pytest.approx(79.41, abs=0.01)
    assert state.compliance_status is ComplianceStatus.PASS
    assert any(e.node == "HUMAN_REVIEW" for e in state.trace)


def test_an_edit_recalculates_against_the_new_threshold(monkeypatch):
    # Reviewer replaces the chocolate rule (40%) with the footwear rule (50%).
    # 79.41% clears both, so the verdict stays PASS -- but the threshold must move.
    state, _ = _run(("edit", "6403.99"), ("approve", None), monkeypatch=monkeypatch)
    assert state.hs_code == "6403.99"
    assert state.rvc_threshold == 50.0
    assert state.hs_classification_confidence == 1.0
    assert "human reviewer" in state.hs_classification_rationale
    assert state.final_output is not None
    assert state.final_output["assessment"]["rvc_threshold_percent"] == 50.0


def test_an_edit_triggers_a_genuine_recalculation(monkeypatch):
    """The edit must re-run the maths, not relabel the previous result.

    No rule in the controlled set has a threshold above 79.41%, so this cannot
    demonstrate a PASS flipping to a FAIL. What it does assert is the mechanism: a
    second CALCULATE_RVC event, against the reviewer's threshold.
    """
    state, _ = _run(("edit", "8544.42"), ("approve", None), monkeypatch=monkeypatch)
    assert state.rvc_threshold == 55.0
    recalcs = [e for e in state.trace if e.node == "CALCULATE_RVC"]
    assert len(recalcs) == 2, "the edit must trigger a second calculation, not reuse the first"


def test_the_edit_loop_is_bounded(monkeypatch):
    """A reviewer who keeps editing cannot spin the workflow forever."""
    edits = [("edit", "6403.99")] * (graph.MAX_EDIT_ROUNDS + 1)
    state, _ = _run(*edits, monkeypatch=monkeypatch)
    lookups = [e for e in state.trace if e.node == "LOOKUP_RULE"]
    # One initial lookup plus at most MAX_EDIT_ROUNDS recalculations.
    assert len(lookups) <= graph.MAX_EDIT_ROUNDS + 1
    assert state.final_output is None, "the loop bound must not smuggle in a report"


def test_the_checkpoint_is_reached_on_every_run(monkeypatch):
    """No input should be able to skip the pause."""
    for decision in ("approve", "reject"):
        state, _ = _run((decision, None), monkeypatch=monkeypatch)
        human_events = [e for e in state.trace if e.node == "HUMAN_REVIEW"]
        assert human_events, f"{decision} run never reached the checkpoint"


def test_an_unclassifiable_product_still_pauses_before_anything_is_issued(monkeypatch):
    state, calls = _run(("reject", None), monkeypatch=monkeypatch, hs_code="8541.43")
    # 8541.43 is deliberately absent from the dataset, so the guard must reject it.
    assert state.hs_code == nodes.CLASSIFICATION_UNAVAILABLE
    assert state.rvc_threshold is None
    assert state.compliance_status is ComplianceStatus.NEEDS_HUMAN_REVIEW
    assert any(e.node == "HUMAN_REVIEW" for e in state.trace)
    assert state.final_output is None
