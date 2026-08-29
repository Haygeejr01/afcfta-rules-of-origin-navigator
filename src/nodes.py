"""Workflow nodes.

Division of labour, which is the whole point of the architecture:

  LLM nodes      EXTRACT, CLASSIFY_HS, GENERATE_REPORT
  Pure functions LOOKUP_RULE, CALCULATE_RVC, VERIFY
  Human          HUMAN_REVIEW

The LLM reads unstructured text, picks from a closed list, and writes prose. It
never adds, divides, compares, or looks anything up. Those three verbs are the
observed failure mode of the zero-shot baseline (0/10 on arithmetic while scoring
9/10 on classification), so they are the ones removed from its reach.

Two guards are deterministic rather than prompt-based, because a prompt is a
request and a guard is a guarantee:

  * CLASSIFY_HS validates the model's proposed code against the rule table and
    overrides anything absent to CLASSIFICATION_UNAVAILABLE. A well-behaved reply
    and a hallucinated one are treated identically -- membership in the dataset is
    the only thing that counts.
  * GENERATE_REPORT assembles every numeric field from state. The model's output is
    confined to named prose fields, and those are scanned for percentages that do
    not appear in state.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from llm import LOCAL_MODEL, call_model, parse_json_text
from state import (
    CLASSIFICATION_UNAVAILABLE,
    ComplianceStatus,
    Component,
    HumanDecision,
    NavigatorState,
    TraceEvent,
)
from tools import (
    CALCULATE_RVC,
    LOOKUP_RULE,
    borderline_band_pp,
    classify_origin,
    hs_code_shortlist,
    load_tariff_db,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT_DIR = PROJECT_ROOT / "logs" / "checkpoints"

# Below this self-reported confidence the classification is flagged for the
# reviewer. The number is a review trigger, not a calibrated probability -- see
# the note in CLASSIFY_HS.
LOW_CONFIDENCE_THRESHOLD = 0.5


# =========================================================================== 1


EXTRACT_SYSTEM = (
    "You extract structured data from export manifests. You output JSON only. "
    "You copy costs exactly as written and never compute totals."
)


def _extract_prompt(raw_text: str) -> str:
    return f"""Read the manifest below and return JSON with this exact shape:

{{
  "product_description": "<one clear sentence describing the finished product>",
  "components": [
    {{"component_name": "<short name>", "origin_country": "<country name only>", "cost_usd": <number>}}
  ]
}}

Rules:
- One entry per input line in the bill of materials. Do not merge or split lines.
- "origin_country" must be the COUNTRY ONLY. Write "Ghana", not "Tema, Ghana".
  Drop city, region, port and supplier names.
- "cost_usd" is the number exactly as written, without currency symbols, commas
  or thousands separators. 4,200.00 becomes 4200.00.
- Do NOT add a total, subtotal, or any line that is not in the manifest.
- Do NOT compute anything.

--- MANIFEST ---
{raw_text}
--- END MANIFEST ---
"""


def node_extract(state: NavigatorState, model: str = LOCAL_MODEL) -> NavigatorState:
    """Parse raw manifest text into typed state fields.

    Uses Ollama's JSON mode, which constrains decoding to valid JSON at the grammar
    level. That is why there is no parse-retry loop here: malformed JSON is not a
    transient failure to retry, it is prevented. A response that is valid JSON but
    the wrong *shape* still fails Pydantic validation, and that routes to review.
    """
    started = time.perf_counter()
    result = call_model(
        _extract_prompt(state.raw_text),
        model=model,
        system=EXTRACT_SYSTEM,
        json_mode=True,
        temperature=0.0,
    )
    elapsed = (time.perf_counter() - started) * 1000.0

    if not result.ok:
        return state.logged(
            TraceEvent(
                node="EXTRACT",
                status="error",
                model=model,
                duration_ms=elapsed,
                detail={"error": result.error},
            )
        ).with_updates(
            compliance_status=ComplianceStatus.NEEDS_HUMAN_REVIEW,
            verification_errors=[*state.verification_errors, f"EXTRACT failed: {result.error}"],
        )

    payload = parse_json_text(result.text)
    if payload is None:
        return state.logged(
            TraceEvent(
                node="EXTRACT",
                status="error",
                model=model,
                duration_ms=elapsed,
                detail={"raw": result.text[:500]},
            )
        ).with_updates(
            compliance_status=ComplianceStatus.NEEDS_HUMAN_REVIEW,
            verification_errors=[
                *state.verification_errors,
                "EXTRACT returned output that was not valid JSON.",
            ],
        )

    components: list[Component] = []
    skipped: list[str] = []
    for item in payload.get("components", []):
        try:
            components.append(
                Component(
                    component_name=str(item["component_name"]),
                    origin_country=str(item["origin_country"]),
                    cost_usd=float(item["cost_usd"]),
                )
            )
        except Exception as exc:  # noqa: BLE001 - a bad line is surfaced, not dropped silently
            skipped.append(f"{item!r} ({type(exc).__name__})")

    updated = state.logged(
        TraceEvent(
            node="EXTRACT",
            status="ok" if components else "error",
            model=model,
            duration_ms=elapsed,
            detail={
                "components_extracted": len(components),
                "components_rejected": skipped,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
            },
        )
    )
    errors = list(updated.verification_errors)
    if skipped:
        errors.append(f"EXTRACT produced {len(skipped)} unusable component line(s): {skipped}")

    return updated.with_updates(
        product_description=str(payload.get("product_description", "")).strip(),
        bill_of_materials=[c.model_dump() for c in components],
        verification_errors=errors,
    )


# =========================================================================== 2


CLASSIFY_SYSTEM = (
    "You classify products against a fixed list of tariff rules. You output JSON "
    "only. You may only choose a code from the list you are given."
)


def _classify_prompt(product_description: str, components: list[Component]) -> str:
    shortlist = "\n".join(
        f'  {{"hs_code": "{r["hs_code"]}", "description": "{r["description"]}"}}'
        for r in hs_code_shortlist()
    )
    component_lines = "\n".join(f"  - {c.component_name} ({c.origin_country})" for c in components)
    return f"""Choose the single best-fitting HS code for this product from the list below.

PRODUCT: {product_description}

COMPONENTS:
{component_lines}

THE ONLY CODES YOU MAY CHOOSE FROM:
{shortlist}

If none of these codes genuinely covers this product, return
"CLASSIFICATION_UNAVAILABLE". Returning CLASSIFICATION_UNAVAILABLE is the correct,
expected answer for a product outside the list -- it is not a failure. Do NOT pick
the nearest-looking code as a fallback.

Return JSON:
{{
  "hs_code": "<code from the list, or CLASSIFICATION_UNAVAILABLE>",
  "confidence": <number between 0 and 1>,
  "rationale": "<one sentence, max 30 words>"
}}
"""


def node_classify_hs(state: NavigatorState, model: str = LOCAL_MODEL) -> NavigatorState:
    """Pick an HS code from the controlled shortlist, or return the sentinel.

    The prompt asks for a code from the list. The code below *enforces* it: whatever
    comes back is checked for membership in the rule table, and anything absent
    becomes CLASSIFICATION_UNAVAILABLE. This is the guard the baseline lacked -- it
    answered a solar PV module with 8544.42, the adjacent electrical-conductor rule,
    which is precisely the plausible-looking guess that this override converts into
    a routing decision.

    ``confidence`` is the model's self-report. It is deliberately not used to gate
    anything numeric; it only raises a flag for the reviewer, because a
    self-assessed number is not a calibrated probability.
    """
    started = time.perf_counter()
    result = call_model(
        _classify_prompt(state.product_description, state.bill_of_materials),
        model=model,
        system=CLASSIFY_SYSTEM,
        json_mode=True,
        temperature=0.0,
    )
    elapsed = (time.perf_counter() - started) * 1000.0

    known_codes = set(load_tariff_db()["rules"].keys())

    if not result.ok:
        return state.logged(
            TraceEvent(
                node="CLASSIFY_HS",
                status="error",
                model=model,
                duration_ms=elapsed,
                detail={"error": result.error},
            )
        ).with_updates(
            hs_code=CLASSIFICATION_UNAVAILABLE,
            hs_classification_confidence=0.0,
            hs_classification_rationale=f"Classification call failed: {result.error}",
        )

    payload = parse_json_text(result.text) or {}
    proposed_raw = str(payload.get("hs_code", "")).strip()
    proposed = _normalise_hs_code(proposed_raw)

    try:
        confidence = max(0.0, min(1.0, float(payload.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0

    rationale = str(payload.get("rationale", "")).strip()[:300]

    if proposed in known_codes:
        final_code = proposed
        status = "ok"
        override = None
    else:
        final_code = CLASSIFICATION_UNAVAILABLE
        status = "unavailable"
        override = proposed_raw or "(empty)"
        if proposed_raw and proposed_raw.upper() != CLASSIFICATION_UNAVAILABLE:
            rationale = (
                f"Model proposed {proposed_raw!r}, which is not in the controlled rule "
                f"dataset; overridden to CLASSIFICATION_UNAVAILABLE. "
                f"Model rationale was: {rationale}"
            )
            # A code that had to be overridden tells us nothing reliable about
            # confidence in the *final* answer, so it is not carried forward.
            confidence = 0.0

    return state.logged(
        TraceEvent(
            node="CLASSIFY_HS",
            status=status,
            model=model,
            duration_ms=elapsed,
            detail={
                "proposed_by_model": proposed_raw,
                "accepted": final_code,
                "overridden_because_not_in_dataset": override
                if final_code == CLASSIFICATION_UNAVAILABLE and override not in (None, CLASSIFICATION_UNAVAILABLE)
                else None,
                "self_reported_confidence": confidence,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
            },
        )
    ).with_updates(
        hs_code=final_code,
        hs_classification_confidence=confidence,
        hs_classification_rationale=rationale,
    )


def _normalise_hs_code(code: str) -> str:
    """Accept ``180632``, ``1806.32`` and ``1806 32`` as the same rule key."""
    if code.upper() == CLASSIFICATION_UNAVAILABLE:
        return CLASSIFICATION_UNAVAILABLE
    digits = re.sub(r"\D", "", code)
    return f"{digits[:4]}.{digits[4:6]}" if len(digits) >= 6 else code.strip()


# =========================================================================== 3


def node_lookup_rule(state: NavigatorState) -> NavigatorState:
    """Deterministic: HS code -> RVC threshold. No LLM, no network."""
    started = time.perf_counter()
    lookup = LOOKUP_RULE(state.hs_code)
    elapsed = (time.perf_counter() - started) * 1000.0

    return state.logged(
        TraceEvent(
            node="LOOKUP_RULE",
            status="ok" if lookup.found else "not_found",
            model=None,
            duration_ms=elapsed,
            detail={
                "hs_code": state.hs_code,
                "rvc_threshold": lookup.rvc_threshold,
                "reason": lookup.reason,
            },
        )
    ).with_updates(
        rvc_threshold=lookup.rvc_threshold,
        rule_description=lookup.description,
    )


# =========================================================================== 4


def node_calculate_rvc(state: NavigatorState) -> NavigatorState:
    """Deterministic: the only place a percentage is ever produced."""
    started = time.perf_counter()
    calc = CALCULATE_RVC(state.bill_of_materials, rvc_threshold=state.rvc_threshold)
    elapsed = (time.perf_counter() - started) * 1000.0

    return state.logged(
        TraceEvent(
            node="CALCULATE_RVC",
            status="ok",
            model=None,
            duration_ms=elapsed,
            detail={
                "total_cost": calc.total_cost,
                "african_sourced_cost": calc.african_sourced_cost,
                "rvc_percent": calc.rvc_percent,
                "threshold": calc.rvc_threshold,
                "margin_pp": calc.margin_pp,
                "status": calc.compliance_status.value,
                "basis": calc.basis,
                "unrecognized_origins": calc.unrecognized_origins,
            },
        )
    ).with_updates(
        total_cost=calc.total_cost,
        african_sourced_cost=calc.african_sourced_cost,
        non_african_sourced_cost=calc.non_african_sourced_cost,
        calculated_rvc_percent=calc.rvc_percent,
        compliance_status=calc.compliance_status,
        unrecognized_origins=calc.unrecognized_origins,
    )


# =========================================================================== 5


def node_verify(state: NavigatorState) -> NavigatorState:
    """Confirm the deterministic output is internally consistent.

    This node earns its place by protecting the scarcest resource in the system --
    reviewer attention. It separates two kinds of finding:

      errors   structurally impossible state. Forces NEEDS_HUMAN_REVIEW; the
               assessment cannot be trusted at all.
      warnings state that is coherent but deserves a human's eye (an unmapped
               origin country, low self-reported classification confidence, a
               result inside the borderline band).

    It recomputes the totals from the bill of materials rather than trusting the
    fields already in state, so a node that wrote an inconsistent number is caught
    here rather than in the report.
    """
    started = time.perf_counter()
    errors: list[str] = []
    warnings: list[str] = []

    if not state.bill_of_materials:
        errors.append("Bill of materials is empty; nothing could be assessed.")

    for idx, component in enumerate(state.bill_of_materials, start=1):
        if component.cost_usd < 0:
            errors.append(f"Component {idx} ({component.component_name}) has a negative cost.")
        if not component.origin_country.strip():
            errors.append(f"Component {idx} ({component.component_name}) has no origin country.")

    recomputed_total = round(sum(c.cost_usd for c in state.bill_of_materials), 2)
    if state.bill_of_materials and abs(recomputed_total - state.total_cost) > 0.01:
        errors.append(
            f"total_cost ({state.total_cost}) does not match the sum of components "
            f"({recomputed_total})."
        )

    if state.total_cost <= 0 and state.bill_of_materials:
        errors.append("Total cost is zero or negative, so no percentage is meaningful.")

    split = round(state.african_sourced_cost + state.non_african_sourced_cost, 2)
    if state.bill_of_materials and abs(split - state.total_cost) > 0.01:
        errors.append(
            f"Regional ({state.african_sourced_cost}) and non-regional "
            f"({state.non_african_sourced_cost}) costs do not sum to the total "
            f"({state.total_cost})."
        )

    if state.calculated_rvc_percent is not None and not (
        0.0 <= state.calculated_rvc_percent <= 100.0
    ):
        errors.append(
            f"calculated_rvc_percent ({state.calculated_rvc_percent}) is outside [0, 100]."
        )

    known_codes = set(load_tariff_db()["rules"].keys())
    if state.hs_code is None:
        errors.append("No HS code was set.")
    elif state.hs_code != CLASSIFICATION_UNAVAILABLE and state.hs_code not in known_codes:
        errors.append(
            f"hs_code {state.hs_code!r} is not in the controlled dataset and is not the "
            f"CLASSIFICATION_UNAVAILABLE sentinel."
        )

    if state.is_classified:
        expected = LOOKUP_RULE(state.hs_code).rvc_threshold
        if state.rvc_threshold is None:
            errors.append(f"HS code {state.hs_code} is known but no threshold was loaded.")
        elif expected is not None and abs(expected - state.rvc_threshold) > 1e-9:
            errors.append(
                f"rvc_threshold ({state.rvc_threshold}) does not match the dataset value "
                f"for {state.hs_code} ({expected})."
            )
    elif state.rvc_threshold is not None:
        errors.append(
            "A threshold was set even though the product is unclassified; no rule applies."
        )

    if state.compliance_status is None:
        errors.append("No compliance status was set.")

    # --- warnings ---------------------------------------------------------
    if state.unrecognized_origins:
        warnings.append(
            "These origin values could not be interpreted as a country and were "
            f"excluded from regional content: {', '.join(state.unrecognized_origins)}. "
            "The calculated percentage may be understated."
        )
    if state.is_classified and state.hs_classification_confidence < LOW_CONFIDENCE_THRESHOLD:
        warnings.append(
            f"Self-reported classification confidence is low "
            f"({state.hs_classification_confidence:.2f}); confirm the HS code."
        )
    if state.compliance_status is ComplianceStatus.BORDERLINE:
        warnings.append(
            f"Result is inside the +/-{borderline_band_pp()}pp borderline band; "
            "the pass/fail call needs human judgement."
        )
    if state.hs_code == CLASSIFICATION_UNAVAILABLE:
        warnings.append(
            "No rule in the controlled dataset covers this product, so no threshold "
            "comparison was made. A reviewer must supply the applicable HS code."
        )
    if not state.product_description.strip():
        warnings.append("No product description was extracted.")

    elapsed = (time.perf_counter() - started) * 1000.0
    combined = [*state.verification_errors, *errors]

    updated = state.logged(
        TraceEvent(
            node="VERIFY",
            status="error" if combined else ("warn" if warnings else "ok"),
            model=None,
            duration_ms=elapsed,
            detail={"errors": combined, "warnings": warnings},
        )
    ).with_updates(verification_errors=combined)

    if combined:
        # Structurally broken state must not carry a PASS/FAIL verdict into review.
        return updated.with_updates(compliance_status=ComplianceStatus.NEEDS_HUMAN_REVIEW)
    return updated


def verification_warnings(state: NavigatorState) -> list[str]:
    """Pull the warning list off the most recent VERIFY trace event."""
    for event in reversed(state.trace):
        if event.node == "VERIFY":
            return list(event.detail.get("warnings", []))
    return []


# =========================================================================== 6


def save_checkpoint(state: NavigatorState) -> Path:
    """Persist state to disk so the pause survives process exit.

    The human checkpoint is a real pause, not a prompt inside one process's memory.
    Writing the full state here is what makes the interrupt resumable.
    """
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    path = CHECKPOINT_DIR / f"{state.manifest_id or 'adhoc'}.checkpoint.json"
    path.write_text(json.dumps(state.model_dump(), indent=2, default=str), encoding="utf-8")
    return path


def load_checkpoint(manifest_id: str) -> Optional[NavigatorState]:
    path = CHECKPOINT_DIR / f"{manifest_id}.checkpoint.json"
    if not path.exists():
        return None
    return NavigatorState.model_validate(json.loads(path.read_text(encoding="utf-8")))


def render_review_panel(state: NavigatorState) -> str:
    """The text the reviewer actually reads. Every number comes from state."""
    band = borderline_band_pp()
    width = 74
    lines = [
        "=" * width,
        "  HUMAN REVIEW REQUIRED — execution is paused".ljust(width),
        "=" * width,
        "",
        f"  Manifest        : {state.manifest_id or '(ad hoc)'}",
        f"  Product         : {state.product_description or '(none extracted)'}",
        "",
        f"  HS code         : {state.hs_code}",
    ]
    if state.rule_description:
        lines.append(f"  Rule            : {state.rule_description}")
    if state.is_classified:
        lines.append(
            f"  Confidence      : {state.hs_classification_confidence:.2f} (model self-reported)"
        )
    lines += [
        "",
        f"  Components      : {len(state.bill_of_materials)}",
        f"  Total cost      : USD {state.total_cost:,.2f}",
        f"  Regional cost   : USD {state.african_sourced_cost:,.2f}",
        f"  Non-regional    : USD {state.non_african_sourced_cost:,.2f}",
        "",
    ]

    if state.calculated_rvc_percent is None:
        lines.append("  RVC             : not computable from the supplied data")
    elif state.rvc_threshold is None:
        lines.append(
            f"  RVC             : {state.calculated_rvc_percent:.2f}%  "
            f"(no threshold — product not covered by the dataset)"
        )
    else:
        margin = state.calculated_rvc_percent - state.rvc_threshold
        lines.append(
            f"  RVC             : {state.calculated_rvc_percent:.2f}%  vs threshold "
            f"{state.rvc_threshold:.2f}%   ({margin:+.2f} pp)"
        )
        if abs(margin) <= band:
            lines.append(f"                    ^ inside the +/-{band}pp borderline band")

    lines += ["", f"  STATUS          : {state.compliance_status.value if state.compliance_status else 'UNSET'}"]

    if state.bill_of_materials:
        lines += ["", "  Bill of materials:"]
        for component in state.bill_of_materials:
            match = classify_origin(component.origin_country)
            marker = {
                "eligible": "REGIONAL",
                "external": "imported",
                "unknown": "?? UNKNOWN",
            }[match.kind]
            shown = match.canonical or component.origin_country
            lines.append(
                f"    {component.component_name[:34]:<34} {shown[:16]:<16} "
                f"{component.cost_usd:>11,.2f}  {marker}"
            )

    if state.verification_errors:
        lines += ["", "  VERIFICATION ERRORS (assessment cannot be trusted):"]
        lines += [f"    ! {e}" for e in state.verification_errors]

    warnings = verification_warnings(state)
    if warnings:
        lines += ["", "  Points needing your attention:"]
        lines += [f"    * {w}" for w in warnings]

    lines += ["", "=" * width]
    return "\n".join(lines)


def _terminal_decision_provider(state: NavigatorState) -> tuple[HumanDecision, str, Optional[str]]:
    """Block on real terminal input. Returns (decision, notes, edited_hs_code)."""
    known_codes = sorted(load_tariff_db()["rules"].keys())
    while True:
        print("\n  [approve] issue the draft report with these findings")
        print("  [reject]  stop; do not issue a report")
        print("  [edit]    correct the HS code, then recalculate")
        try:
            raw = input("\n  Your decision (approve/reject/edit): ").strip().lower()
        except EOFError:
            print("\n  No input stream available; treating as reject.")
            return HumanDecision.REJECT, "No interactive input was available.", None

        if raw in ("approve", "a"):
            notes = input("  Optional note for the report: ").strip()
            return HumanDecision.APPROVE, notes, None
        if raw in ("reject", "r"):
            notes = input("  Reason for rejection: ").strip()
            return HumanDecision.REJECT, notes, None
        if raw in ("edit", "e"):
            print(f"\n  Codes in the controlled dataset: {', '.join(known_codes)}")
            code = _normalise_hs_code(input("  Corrected HS code: ").strip())
            if code not in known_codes:
                print(
                    f"  {code!r} is not in the controlled dataset. The dataset is the "
                    f"only source of rules, so it cannot be used."
                )
                continue
            notes = input("  Note explaining the correction: ").strip()
            return HumanDecision.EDIT, notes, code
        print("  Please answer approve, reject, or edit.")


DecisionProvider = Callable[[NavigatorState], tuple[HumanDecision, str, Optional[str]]]


def node_human_review(
    state: NavigatorState,
    decision_provider: Optional[DecisionProvider] = None,
    *,
    show_panel: bool = True,
) -> tuple[NavigatorState, Optional[str]]:
    """Pause for explicit human approval. Returns (state, edited_hs_code).

    ``decision_provider`` defaults to blocking terminal input. The evaluation
    harness injects a scripted provider so accuracy can be measured
    non-interactively; that substitution is labelled in the eval output, and the
    CLI path always uses the real interrupt.
    """
    checkpoint = save_checkpoint(state)
    if show_panel:
        print()
        print(render_review_panel(state))

    provider = decision_provider or _terminal_decision_provider
    started = time.perf_counter()
    decision, notes, edited_code = provider(state)
    elapsed = (time.perf_counter() - started) * 1000.0

    updated = state.logged(
        TraceEvent(
            node="HUMAN_REVIEW",
            status=decision.value,
            model=None,
            duration_ms=elapsed,
            detail={
                "decision": decision.value,
                "notes": notes,
                "edited_hs_code": edited_code,
                "checkpoint": str(checkpoint),
                "seconds_waiting_for_human": round(elapsed / 1000.0, 1),
            },
        )
    ).with_updates(human_decision=decision, human_notes=notes)

    save_checkpoint(updated)
    return updated, edited_code


# =========================================================================== 7


REPORT_SYSTEM = (
    "You write short, plain-language trade compliance summaries for small "
    "exporters. You output JSON only. You never state or restate a number: the "
    "figures are inserted by the system, not by you."
)


def _report_prompt(state: NavigatorState) -> str:
    status = state.compliance_status.value if state.compliance_status else "NEEDS_HUMAN_REVIEW"
    warnings = verification_warnings(state)
    return f"""Write the narrative sections of a draft compliance assessment for this
already-decided assessment. The decision and all figures are FIXED and will be
inserted by the system. Your job is wording only.

Assessment (for context — do not restate the numbers):
- Product: {state.product_description}
- HS code applied: {state.hs_code}
- Rule: {state.rule_description or "no rule in the controlled dataset covers this product"}
- Outcome: {status}
- Reviewer note: {state.human_notes or "(none)"}
- Points flagged during verification: {"; ".join(warnings) if warnings else "(none)"}

Return JSON:
{{
  "summary": "<2-3 sentences explaining the outcome in plain language for a small business owner>",
  "what_this_means": "<2-3 sentences on the practical implication for their export>",
  "recommended_next_steps": ["<step>", "<step>", "<step>"],
  "limitations": "<1-2 sentences noting this is an assessment against a controlled dataset, not a legal determination>"
}}

Hard constraints:
- Do NOT write any percentage, cost, or figure. Refer to them in words only, e.g.
  "the calculated regional content" or "the applicable threshold".
- Do NOT claim the product is legally certified, approved, or cleared by any authority.
- Write for a reader with no customs expertise.
"""


_NUMERIC_PATTERN = re.compile(r"\d+(?:[.,]\d+)?\s*%|(?:USD|\$)\s*[\d,]+(?:\.\d+)?")

# Affirmative assurances the narrative must never make. The prompt already forbids
# them (see REPORT_SYSTEM); on the first live M001 run the model wrote "You can now
# export your chocolate bars without any issues related to trade regulations"
# anyway. That is a legal assurance this system has no standing to give, and it
# contradicted the disclaimer printed directly beneath it. The prompt was a request;
# this is the check. See CHANGELOG.
#
# Only affirmative constructions are listed. Bare words like "legal" or "certified"
# are deliberately absent, because the narrative is *asked* for a limitations
# sentence saying this is "not a legal determination" and flagging that would train
# the reader to ignore the flag.
_OVERCLAIM_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"without any (?:issues|problems|restrictions|barriers)", re.I),
     "unqualified assurance"),
    (re.compile(r"\bno (?:issues|problems|barriers|restrictions"
                r"|further (?:checks?|action|review|verification))\b", re.I),
     "tells the reader to stop verifying"),
    (re.compile(r"\b(?:you (?:can|may) now|free to)\s+(?:export|ship|file|claim)\w*", re.I),
     "grants a permission this system cannot grant"),
    (re.compile(r"\b(?:can|may) continue (?:to )?(?:export|ship)\w*", re.I),
     "grants a permission this system cannot grant"),
    # The trailing \w* is for the reviewer, not the regex: the matched text is printed
    # in the report, and "to be export" reads as a truncation bug.
    (re.compile(r"\bmeets?(?: all)? the (?:criteria|requirements|conditions)\s+"
                r"(?:to be |for )?(?:export|ship)\w*", re.I),
     "asserts export eligibility broader than what was assessed"),
    (re.compile(r"\bmeets?\s+(?:all\s+)?(?:the\s+)?(?:necessary|required|applicable)\s+"
                r"(?:\w+\s+){0,2}requirements\b", re.I),
     "asserts compliance with requirements this system never checked"),
    (re.compile(r"\bcompl(?:y|ies|ied|iant|iance)\s+with\s+(?:the\s+|all\s+)*"
                r"(?:trade\s+)?regulations\b", re.I),
     "asserts general regulatory compliance from a single RVC check"),
    (re.compile(r"\b(?:is|are)\s+(?:now\s+)?(?:legally\s+)?"
                r"(?:certified|approved|cleared|authoris\w+|authoriz\w+)\b", re.I),
     "asserts a decision only an authority can make"),
    (re.compile(r"\bwill (?:be )?(?:accepted|approved|cleared)\b", re.I),
     "predicts an authority's decision"),
    (re.compile(r"\bguarantee(?:s|d|ing)?\b", re.I), "guarantee"),
    (re.compile(r"\bfully compliant\b|\bcompliant with all\b", re.I),
     "unqualified compliance claim"),
    (re.compile(r"\bduty[-\s]free\b", re.I), "asserts a tariff outcome"),
    (re.compile(r"\blegally binding\b", re.I), "asserts legal force"),
    (re.compile(r"\bcertificate of origin\b", re.I), "names a document this is not"),
]

# "This is NOT a certificate of origin" is the correct sentence, so a match whose
# immediately preceding text negates it is not a hit.
_NEGATION_BEFORE = re.compile(r"\b(?:not|no|never|cannot|can't|isn't|aren't|without)\b[^.]{0,40}$", re.I)


def _scan_for_invented_figures(narrative: dict[str, Any]) -> list[str]:
    """Flag any figure the model wrote into prose.

    The report's numeric fields are assembled from state, so a number appearing in
    the narrative cannot change the assessment -- but it can mislead a reader. The
    prompt forbids figures; this check verifies the prompt was obeyed instead of
    assuming it. A hit is recorded in the report metadata rather than silently
    dropped, because "the model ignored the instruction" is worth knowing.
    """
    hits: list[str] = []
    for field, value in narrative.items():
        text = " ".join(value) if isinstance(value, list) else str(value)
        for match in _NUMERIC_PATTERN.findall(text):
            hits.append(f"{field}: {match.strip()}")
    return hits


def _scan_for_overclaims(narrative: dict[str, Any]) -> list[str]:
    """Flag prose that asserts legal standing this assessment does not have.

    The figure scan protects the *numbers*; nothing protected the *claims*. The
    narrative is generated after the human has already approved the assessment, so
    an overclaim reaches the document without any reviewer ever reading it. This
    scan is what makes that visible.

    Known limits, stated rather than implied: it is a phrase list, so it catches the
    constructions observed in real runs and paraphrases close to them, not every
    possible way of overstating a result. It bounds recognised phrasings, not
    meaning -- the same distinction the HS membership guard runs into.

    The list was widened once, on evidence: a second live run (M008, soap) produced
    three variants the first pass missed -- "can continue exporting", "comply with the
    regulations", "meet the necessary trade compliance requirements". Both real
    narratives are pinned as test fixtures, so the list can only grow against observed
    output rather than imagined output.
    """
    hits: list[str] = []
    for field, value in narrative.items():
        text = " ".join(value) if isinstance(value, list) else str(value)
        for pattern, why in _OVERCLAIM_PATTERNS:
            for match in pattern.finditer(text):
                if _NEGATION_BEFORE.search(text[: match.start()]):
                    continue
                hits.append(f"{field}: {match.group(0).strip()!r} — {why}")
    return hits


def node_generate_report(state: NavigatorState, model: str = LOCAL_MODEL) -> NavigatorState:
    """Assemble the final document. Numbers come from state; the LLM writes prose only.

    The structure is the enforcement mechanism. ``assessment`` is built from state
    fields in this function, and the model's output is confined to ``narrative``.
    There is no code path by which a model token becomes a reported figure.
    """
    started = time.perf_counter()
    result = call_model(
        _report_prompt(state), model=model, system=REPORT_SYSTEM, json_mode=True, temperature=0.2
    )
    elapsed = (time.perf_counter() - started) * 1000.0

    payload = parse_json_text(result.text) or {}

    def _text(key: str) -> str:
        """Coerce a narrative field to a string, treating JSON null as absent.

        ``str(None)`` is ``"None"`` -- truthy, and it would sail past the
        ``narrative_ok`` check straight into the document as the summary. Caught by
        tests/test_report_integrity.py.
        """
        value = payload.get(key)
        return "" if value is None else str(value).strip()

    narrative = {
        "summary": _text("summary"),
        "what_this_means": _text("what_this_means"),
        "recommended_next_steps": [
            str(s).strip()
            for s in (payload.get("recommended_next_steps") or [])
            if s is not None and str(s).strip()
        ],
        "limitations": _text("limitations"),
    }
    narrative_ok = bool(narrative["summary"])
    invented = _scan_for_invented_figures(narrative)
    overclaims = _scan_for_overclaims(narrative)

    status = state.compliance_status.value if state.compliance_status else "NEEDS_HUMAN_REVIEW"
    band = borderline_band_pp()

    final_output = {
        "document_title": "Draft Compliance Assessment Report — Human Approved",
        "document_type": "draft_assessment",
        "manifest_id": state.manifest_id,
        "product_description": state.product_description,
        # ---- every figure below is copied from state, never regenerated ----
        "assessment": {
            "hs_code": state.hs_code,
            "hs_code_source": (
                "selected from the controlled dataset"
                if state.is_classified
                else "not available in the controlled dataset"
            ),
            "rule_description": state.rule_description,
            "rvc_threshold_percent": state.rvc_threshold,
            "total_cost_usd": state.total_cost,
            "regionally_sourced_cost_usd": state.african_sourced_cost,
            "non_regionally_sourced_cost_usd": state.non_african_sourced_cost,
            "calculated_rvc_percent": state.calculated_rvc_percent,
            "margin_vs_threshold_pp": (
                round(state.calculated_rvc_percent - state.rvc_threshold, 2)
                if state.calculated_rvc_percent is not None and state.rvc_threshold is not None
                else None
            ),
            "borderline_band_pp": band,
            "compliance_status": status,
            "calculation_method": (
                "Build-up, materials-only cost base: regionally sourced component cost "
                "divided by total component cost. Computed by a deterministic function, "
                "not by a language model."
            ),
        },
        "bill_of_materials": [c.model_dump() for c in state.bill_of_materials],
        "human_review": {
            "decision": state.human_decision.value if state.human_decision else None,
            "reviewer_notes": state.human_notes,
            "reviewed_before_issue": True,
        },
        "narrative": narrative,
        "verification": {
            "errors": state.verification_errors,
            "warnings": verification_warnings(state),
        },
        "disclaimer": (
            "This is a draft assessment produced against a controlled set of AfCFTA "
            "rule data for guidance only. It is not a legal determination of origin, "
            "not a certificate of origin, and carries no authority. Thresholds in the "
            "underlying dataset are synthetic and illustrative. Confirm any claim with "
            "your national customs authority or a licensed customs broker before "
            "filing."
        ),
        "metadata": {
            "narrative_model": model,
            "narrative_generated": narrative_ok,
            "figures_written_by_model_in_prose": invented,
            "claims_flagged_in_prose": overclaims,
            "model_calls_this_run": state.model_call_count + 1,
        },
    }

    if not narrative_ok:
        final_output["narrative"]["summary"] = (
            "The narrative section could not be generated. The assessment figures "
            "above are unaffected: they are produced by deterministic calculation and "
            "were reviewed and approved by a human before this document was issued."
        )

    return state.logged(
        TraceEvent(
            node="GENERATE_REPORT",
            status="ok" if (narrative_ok and not overclaims) else "warn",
            model=model,
            duration_ms=elapsed,
            detail={
                "narrative_generated": narrative_ok,
                "figures_written_by_model_in_prose": invented,
                "claims_flagged_in_prose": overclaims,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
            },
        )
    ).with_updates(final_output=final_output)
