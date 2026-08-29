"""Standalone tests for the deterministic tools.

Every case here uses plain dicts and literal numbers -- no Pydantic models, no
LLM, no network. If these pass, the arithmetic and lookup contracts hold
independently of anything the model does.
"""

import pytest

from state import CLASSIFICATION_UNAVAILABLE, ComplianceStatus
from tools import (
    CALCULATE_RVC,
    LOOKUP_RULE,
    borderline_band_pp,
    classify_origin,
    resolve_origin,
)


# ----------------------------------------------------------------- LOOKUP_RULE


def test_lookup_returns_threshold_for_known_code():
    result = LOOKUP_RULE("1806.32")
    assert result.found is True
    assert result.rvc_threshold == 40.0
    assert "cocoa" in result.description.lower()


def test_lookup_rejects_unknown_code_without_defaulting():
    result = LOOKUP_RULE("8541.43")  # solar modules: deliberately absent
    assert result.found is False
    assert result.rvc_threshold is None, "an unknown code must never get a default threshold"
    assert "not present" in result.reason


def test_lookup_handles_classification_unavailable_sentinel():
    result = LOOKUP_RULE(CLASSIFICATION_UNAVAILABLE)
    assert result.found is False
    assert result.rvc_threshold is None


def test_lookup_handles_none():
    assert LOOKUP_RULE(None).found is False


def test_every_threshold_in_dataset_is_in_the_documented_band():
    from tools import load_tariff_db

    for code, rule in load_tariff_db()["rules"].items():
        assert 30.0 <= rule["rvc_threshold"] <= 60.0, f"{code} threshold out of band"


# --------------------------------------------------------------- origin matching


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Ghana", "Ghana"),
        ("  ghana  ", "Ghana"),
        ("Cote d'Ivoire", "Cote d'Ivoire"),
        ("Côte d'Ivoire", "Cote d'Ivoire"),
        ("Ivory Coast", "Cote d'Ivoire"),
        ("Tema, Ghana", "Ghana"),
        ("tanned Kano Nigeria", "Nigeria"),
        ("RSA", "South Africa"),
        ("China", None),
        ("Germany", None),
        ("", None),
        ("Atlantis", None),
    ],
)
def test_resolve_origin(raw, expected):
    assert resolve_origin(raw) == expected


@pytest.mark.parametrize(
    "raw,kind,canonical",
    [
        ("Ghana", "eligible", "Ghana"),
        ("Ivory Coast", "eligible", "Cote d'Ivoire"),
        ("Tema, Ghana", "eligible", "Ghana"),
        # Recognised imports must NOT be flagged: they are correct, routine data.
        ("China", "external", "China"),
        ("Germany", "external", "Germany"),
        ("Netherlands", "external", "Netherlands"),
        ("UAE", "external", "United Arab Emirates"),
        ("South Korea", "external", "South Korea"),
        ("imported UAE", "external", "United Arab Emirates"),
        # Only genuinely uninterpretable strings are flagged.
        ("Wakanda", "unknown", None),
        ("", "unknown", None),
        ("various", "unknown", None),
    ],
)
def test_classify_origin_separates_imported_from_uninterpretable(raw, kind, canonical):
    match = classify_origin(raw)
    assert match.kind == kind, f"{raw!r} classified as {match.kind}"
    assert match.canonical == canonical


def test_routine_imports_do_not_raise_a_review_flag():
    """Regression: the first live run flagged China/Germany/Netherlands as
    unrecognised, burying real signal under routine imports."""
    bom = [
        {"component_name": "cocoa", "origin_country": "Ghana", "cost_usd": 8100.0},
        {"component_name": "milk", "origin_country": "Netherlands", "cost_usd": 1400.0},
        {"component_name": "lecithin", "origin_country": "Germany", "cost_usd": 180.0},
        {"component_name": "foil", "origin_country": "China", "cost_usd": 520.0},
    ]
    result = CALCULATE_RVC(bom, rvc_threshold=40.0)
    assert result.unrecognized_origins == [], "recognised imports must not be flagged"
    assert result.african_sourced_cost == 8100.0


# --------------------------------------------------------------- CALCULATE_RVC


def test_simple_pass():
    bom = [
        {"component_name": "cocoa mass", "origin_country": "Ghana", "cost_usd": 700.0},
        {"component_name": "milk powder", "origin_country": "Netherlands", "cost_usd": 300.0},
    ]
    result = CALCULATE_RVC(bom, rvc_threshold=40.0)
    assert result.total_cost == 1000.0
    assert result.african_sourced_cost == 700.0
    assert result.non_african_sourced_cost == 300.0
    assert result.rvc_percent == 70.0
    assert result.compliance_status is ComplianceStatus.PASS


def test_simple_fail():
    bom = [
        {"component_name": "fabric", "origin_country": "China", "cost_usd": 900.0},
        {"component_name": "labels", "origin_country": "Egypt", "cost_usd": 100.0},
    ]
    result = CALCULATE_RVC(bom, rvc_threshold=45.0)
    assert result.rvc_percent == 10.0
    assert result.compliance_status is ComplianceStatus.FAIL


def test_borderline_just_above_threshold_is_not_reported_as_pass():
    bom = [
        {"component_name": "leather", "origin_country": "Egypt", "cost_usd": 510.0},
        {"component_name": "soles", "origin_country": "China", "cost_usd": 490.0},
    ]
    result = CALCULATE_RVC(bom, rvc_threshold=50.0)
    assert result.rvc_percent == 51.0
    assert result.compliance_status is ComplianceStatus.BORDERLINE
    assert result.margin_pp == 1.0


def test_borderline_just_below_threshold_is_not_reported_as_fail():
    bom = [
        {"component_name": "flake", "origin_country": "South Africa", "cost_usd": 340.0},
        {"component_name": "resin", "origin_country": "UAE", "cost_usd": 660.0},
    ]
    result = CALCULATE_RVC(bom, rvc_threshold=35.0)
    assert result.rvc_percent == 34.0
    assert result.compliance_status is ComplianceStatus.BORDERLINE
    assert result.margin_pp == -1.0


def test_exactly_on_threshold_is_borderline_not_pass():
    bom = [
        {"component_name": "a", "origin_country": "Kenya", "cost_usd": 500.0},
        {"component_name": "b", "origin_country": "China", "cost_usd": 500.0},
    ]
    result = CALCULATE_RVC(bom, rvc_threshold=50.0)
    assert result.rvc_percent == 50.0
    assert result.compliance_status is ComplianceStatus.BORDERLINE


def test_just_outside_the_band_is_a_clean_verdict():
    band = borderline_band_pp()
    assert band == 2.0
    bom = [
        {"component_name": "a", "origin_country": "Kenya", "cost_usd": 5300.0},
        {"component_name": "b", "origin_country": "China", "cost_usd": 4700.0},
    ]
    result = CALCULATE_RVC(bom, rvc_threshold=50.0)
    assert result.rvc_percent == 53.0
    assert result.compliance_status is ComplianceStatus.PASS


def test_no_threshold_computes_percentage_but_asserts_no_verdict():
    bom = [
        {"component_name": "frame", "origin_country": "South Africa", "cost_usd": 1600.0},
        {"component_name": "cells", "origin_country": "China", "cost_usd": 11400.0},
    ]
    result = CALCULATE_RVC(bom, rvc_threshold=None)
    assert result.rvc_percent == 12.31
    assert result.rvc_threshold is None
    assert result.compliance_status is ComplianceStatus.NEEDS_HUMAN_REVIEW


def test_empty_bom_does_not_divide_by_zero():
    result = CALCULATE_RVC([], rvc_threshold=40.0)
    assert result.rvc_percent is None
    assert result.compliance_status is ComplianceStatus.NEEDS_HUMAN_REVIEW


def test_all_zero_cost_bom_does_not_divide_by_zero():
    bom = [{"component_name": "sample", "origin_country": "Ghana", "cost_usd": 0.0}]
    result = CALCULATE_RVC(bom, rvc_threshold=40.0)
    assert result.rvc_percent is None
    assert result.compliance_status is ComplianceStatus.NEEDS_HUMAN_REVIEW


def test_unrecognised_origin_is_surfaced_not_silently_dropped():
    bom = [
        {"component_name": "a", "origin_country": "Ghana", "cost_usd": 500.0},
        {"component_name": "b", "origin_country": "Wakanda", "cost_usd": 500.0},
    ]
    result = CALCULATE_RVC(bom, rvc_threshold=40.0)
    assert "Wakanda" in result.unrecognized_origins
    # It does not count toward regional content...
    assert result.african_sourced_cost == 500.0
    # ...but it is still in the total, so the percentage is honest.
    assert result.total_cost == 1000.0


def test_percentage_never_exceeds_100_when_all_regional():
    bom = [{"component_name": "a", "origin_country": "Rwanda", "cost_usd": 123.45}]
    result = CALCULATE_RVC(bom, rvc_threshold=35.0)
    assert result.rvc_percent == 100.0


def test_reported_percentage_and_verdict_never_disagree():
    """A repeating-decimal case: the rounded number and the verdict must agree.

    1/3 of the cost base is regional -> 33.33%. Against a 33.33% threshold the
    reported figure must not read as a FAIL.
    """
    bom = [
        {"component_name": "a", "origin_country": "Kenya", "cost_usd": 1.0},
        {"component_name": "b", "origin_country": "China", "cost_usd": 2.0},
    ]
    result = CALCULATE_RVC(bom, rvc_threshold=33.33, band_pp=0.0)
    assert result.rvc_percent == 33.33
    assert result.compliance_status is ComplianceStatus.BORDERLINE  # margin exactly 0


def test_build_up_and_build_down_agree_on_this_cost_base():
    """Documented algebraic identity: with a materials-only base the two RVC
    methods cannot disagree. Guards the docstring claim."""
    bom = [
        {"component_name": "a", "origin_country": "Ghana", "cost_usd": 811.0},
        {"component_name": "b", "origin_country": "China", "cost_usd": 1189.0},
    ]
    r = CALCULATE_RVC(bom, rvc_threshold=40.0)
    build_down = (r.total_cost - r.non_african_sourced_cost) / r.total_cost * 100
    assert round(build_down, 2) == r.rvc_percent
