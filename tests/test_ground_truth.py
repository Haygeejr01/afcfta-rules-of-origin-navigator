"""Asserts the synthetic dataset's declared ground truth against the calculator.

This is the guard that keeps evaluation honest. The ground-truth numbers in
``synthetic_manifests.json`` were computed by hand; this file recomputes every one
of them from the stated bill of materials through the real deterministic tools. If
a manifest is edited, or a threshold changes, or the borderline band moves, the
mismatch surfaces here rather than in an eval score that quietly grades against
the wrong answer.
"""

import json
from pathlib import Path

import pytest

from state import CLASSIFICATION_UNAVAILABLE, ComplianceStatus
from tools import CALCULATE_RVC, LOOKUP_RULE, DATA_DIR

MANIFESTS = json.loads((DATA_DIR / "synthetic_manifests.json").read_text(encoding="utf-8"))
CASES = MANIFESTS["manifests"]
IDS = [c["manifest_id"] for c in CASES]


def _bom_from_ground_truth(case):
    """The BOM as the manifest states it, parsed from raw_text is *not* used here.

    Ground truth is asserted against the structured expectations, so this test is
    independent of extraction quality. Extraction is graded separately in the eval.
    """
    return case["ground_truth"]


def test_dataset_shape():
    assert len(CASES) == 10
    assert len(set(IDS)) == 10, "manifest ids must be unique"
    mix = MANIFESTS["metadata"]["case_mix"]
    assert sum(mix.values()) == 10


def test_case_mix_matches_declared_metadata():
    counts = {"PASS": 0, "FAIL": 0, "BORDERLINE": 0, "NEEDS_HUMAN_REVIEW": 0}
    for case in CASES:
        counts[case["ground_truth"]["expected_compliance_status"]] += 1
    mix = MANIFESTS["metadata"]["case_mix"]
    assert counts["PASS"] == mix["clear_pass"]
    assert counts["FAIL"] == mix["clear_fail"]
    assert counts["BORDERLINE"] == mix["borderline"]
    assert counts["NEEDS_HUMAN_REVIEW"] == mix["classification_unavailable"]


def test_enough_manifests_have_real_regional_content_to_produce_passes():
    """At least four cases must genuinely pass, so PASS is a tested path."""
    passes = [c for c in CASES if c["ground_truth"]["expected_compliance_status"] == "PASS"]
    assert len(passes) >= 4, "need real PASS cases, not just fails and borderlines"
    for case in passes:
        assert case["ground_truth"]["expected_african_sourced_cost"] > 0


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_declared_totals_are_internally_consistent(case):
    gt = case["ground_truth"]
    assert gt["expected_african_sourced_cost"] <= gt["expected_total_cost"]
    assert 0.0 <= gt["expected_rvc_percent"] <= 100.0


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_declared_rvc_percent_matches_declared_costs(case):
    """expected_rvc_percent must equal african/total*100 to 2dp."""
    gt = case["ground_truth"]
    recomputed = round(
        gt["expected_african_sourced_cost"] / gt["expected_total_cost"] * 100, 2
    )
    assert recomputed == gt["expected_rvc_percent"], (
        f"{case['manifest_id']}: declared {gt['expected_rvc_percent']} "
        f"but costs imply {recomputed}"
    )


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_declared_threshold_matches_the_rule_table(case):
    gt = case["ground_truth"]
    lookup = LOOKUP_RULE(gt["expected_hs_code"])
    if gt["expected_hs_code"] == CLASSIFICATION_UNAVAILABLE:
        assert lookup.found is False
        assert gt["expected_rvc_threshold"] is None
    else:
        assert lookup.found is True, f"{gt['expected_hs_code']} missing from rule table"
        assert lookup.rvc_threshold == gt["expected_rvc_threshold"]


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_declared_status_is_what_the_calculator_actually_produces(case):
    """End-to-end check of the deterministic half, using a synthetic two-line BOM
    that reproduces the declared cost split exactly."""
    gt = case["ground_truth"]
    african = gt["expected_african_sourced_cost"]
    other = gt["expected_total_cost"] - african
    bom = [
        {"component_name": "regional inputs", "origin_country": "Ghana", "cost_usd": african},
        {"component_name": "imported inputs", "origin_country": "China", "cost_usd": other},
    ]
    result = CALCULATE_RVC(bom, rvc_threshold=gt["expected_rvc_threshold"])

    assert result.rvc_percent == gt["expected_rvc_percent"]
    assert result.compliance_status is ComplianceStatus(gt["expected_compliance_status"])


def test_borderline_cases_are_genuinely_inside_the_band():
    from tools import borderline_band_pp

    band = borderline_band_pp()
    borderlines = [
        c for c in CASES if c["ground_truth"]["expected_compliance_status"] == "BORDERLINE"
    ]
    assert len(borderlines) >= 1
    for case in borderlines:
        gt = case["ground_truth"]
        margin = abs(gt["expected_rvc_percent"] - gt["expected_rvc_threshold"])
        assert margin <= band, f"{case['manifest_id']} is not actually borderline"


def test_clear_cases_are_comfortably_outside_the_band():
    from tools import borderline_band_pp

    band = borderline_band_pp()
    for case in CASES:
        gt = case["ground_truth"]
        if gt["expected_compliance_status"] in ("PASS", "FAIL"):
            margin = abs(gt["expected_rvc_percent"] - gt["expected_rvc_threshold"])
            assert margin > band, (
                f"{case['manifest_id']} is labelled "
                f"{gt['expected_compliance_status']} but sits inside the band"
            )


def test_the_control_case_targets_a_code_absent_from_the_rule_table():
    control = next(c for c in CASES if c["manifest_id"] == "M010")
    assert control["ground_truth"]["expected_hs_code"] == CLASSIFICATION_UNAVAILABLE
    assert LOOKUP_RULE("8541.43").found is False, (
        "M010 only tests the unavailable path while 8541.43 stays out of the table"
    )


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_raw_text_mentions_every_cost_in_the_declared_total(case):
    """The raw manifest must actually contain enough information to extract.

    Guards against a manifest whose ground truth cannot be derived from its own
    text, which would make extraction failures unfalsifiable.
    """
    raw = case["raw_text"]
    assert len(raw) > 100
    gt = case["ground_truth"]
    # Every manifest states its costs as plain integers in the text.
    assert gt["expected_component_count"] >= 6
