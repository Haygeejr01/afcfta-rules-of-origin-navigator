"""Workflow wiring and CLI.

    EXTRACT -> CLASSIFY_HS -> LOOKUP_RULE -> CALCULATE_RVC -> VERIFY -> HUMAN_REVIEW
                                   ^                                        |
                                   |                                        v
                                   +--------- edit (bounded) ------  approve -> GENERATE_REPORT
                                                                     reject  -> stop, no report

Implemented as an explicit Python state machine rather than with a graph framework.
The control flow is one straight line plus a single bounded loop back to
LOOKUP_RULE when the reviewer corrects an HS code, and the pause is a real process
pause backed by a checkpoint file on disk. A framework would add a dependency and
an indirection layer without changing any of that. If the flow later grows
concurrent branches or needs distributed resumption, that is the point to revisit
it -- see CHANGELOG.

The human checkpoint is unconditional: GENERATE_REPORT is unreachable except
through an explicit approve decision.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from llm import LOCAL_MODEL, health_check  # noqa: E402
from nodes import (  # noqa: E402
    DecisionProvider,
    node_calculate_rvc,
    node_classify_hs,
    node_extract,
    node_generate_report,
    node_human_review,
    node_lookup_rule,
    node_verify,
    render_review_panel,
    verification_warnings,
)
from state import ComplianceStatus, HumanDecision, NavigatorState  # noqa: E402
from tools import DATA_DIR, load_tariff_db  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRAJECTORY_DIR = PROJECT_ROOT / "logs" / "trajectories"

# A reviewer correcting a code should not be able to spin forever. This bounds a
# human interaction loop; it is not network retry logic.
MAX_EDIT_ROUNDS = 3


def run_pipeline_to_verify(
    raw_text: str,
    manifest_id: Optional[str] = None,
    model: str = LOCAL_MODEL,
) -> NavigatorState:
    """Everything up to and including VERIFY. No human interaction, no report.

    This is the deterministic, gradeable half of the system. The evaluation harness
    calls exactly this, so accuracy numbers never depend on a scripted human.
    """
    state = NavigatorState(manifest_id=manifest_id, raw_text=raw_text)
    state = node_extract(state, model=model)
    state = node_classify_hs(state, model=model)
    state = node_lookup_rule(state)
    state = node_calculate_rvc(state)
    state = node_verify(state)
    return state


def run_assessment(
    raw_text: str,
    manifest_id: Optional[str] = None,
    model: str = LOCAL_MODEL,
    decision_provider: Optional[DecisionProvider] = None,
    show_panel: bool = True,
    verbose: bool = True,
) -> NavigatorState:
    """The full workflow, including the mandatory human checkpoint."""
    if verbose:
        print(f"\n  Running assessment (model: {model})")
        print("  " + "-" * 70)

    state = NavigatorState(manifest_id=manifest_id, raw_text=raw_text)

    state = _step(state, "EXTRACT", lambda s: node_extract(s, model=model), verbose)
    state = _step(state, "CLASSIFY_HS", lambda s: node_classify_hs(s, model=model), verbose)
    state = _step(state, "LOOKUP_RULE", node_lookup_rule, verbose)
    state = _step(state, "CALCULATE_RVC", node_calculate_rvc, verbose)
    state = _step(state, "VERIFY", node_verify, verbose)

    for round_index in range(MAX_EDIT_ROUNDS + 1):
        state, edited_code = node_human_review(
            state, decision_provider=decision_provider, show_panel=show_panel
        )

        if state.human_decision is HumanDecision.EDIT and edited_code:
            if round_index >= MAX_EDIT_ROUNDS:
                print(f"\n  Edit limit ({MAX_EDIT_ROUNDS}) reached; no further corrections.")
                break
            if verbose:
                print(f"\n  Reviewer corrected the HS code to {edited_code}. Recalculating.")
            state = state.with_updates(
                hs_code=edited_code,
                hs_classification_rationale=(
                    f"HS code set to {edited_code} by the human reviewer."
                ),
                # A human-supplied code is an authoritative input, not a model
                # prediction, so the model's self-reported confidence no longer
                # describes it.
                hs_classification_confidence=1.0,
                verification_errors=[],
            )
            state = _step(state, "LOOKUP_RULE", node_lookup_rule, verbose)
            state = _step(state, "CALCULATE_RVC", node_calculate_rvc, verbose)
            state = _step(state, "VERIFY", node_verify, verbose)
            continue
        break

    if state.human_decision is HumanDecision.APPROVE:
        state = _step(state, "GENERATE_REPORT", lambda s: node_generate_report(s, model=model), verbose)
    elif verbose:
        decision = state.human_decision.value if state.human_decision else "no decision"
        print(f"\n  Reviewer decision: {decision.upper()} — no report was issued.")

    return state


def _step(state: NavigatorState, name: str, fn, verbose: bool) -> NavigatorState:
    if verbose:
        print(f"  -> {name:<16}", end="", flush=True)
    started = time.perf_counter()
    result = fn(state)
    elapsed = (time.perf_counter() - started) * 1000.0
    if verbose:
        print(f" {_step_summary(name, result)}  ({elapsed / 1000:.1f}s)")
    return result


def _step_summary(name: str, state: NavigatorState) -> str:
    if name == "EXTRACT":
        # total_cost is not populated until CALCULATE_RVC, so only the count is
        # meaningful at this point in the run.
        return f"{len(state.bill_of_materials)} components"
    if name == "CLASSIFY_HS":
        return f"{state.hs_code} (confidence {state.hs_classification_confidence:.2f})"
    if name == "LOOKUP_RULE":
        return (
            f"threshold {state.rvc_threshold}%"
            if state.rvc_threshold is not None
            else "no rule in dataset"
        )
    if name == "CALCULATE_RVC":
        pct = state.calculated_rvc_percent
        status = state.compliance_status.value if state.compliance_status else "?"
        return f"{pct if pct is not None else 'n/a'}% -> {status}"
    if name == "VERIFY":
        errors, warnings = len(state.verification_errors), len(verification_warnings(state))
        if errors:
            return f"{errors} error(s), {warnings} warning(s)"
        return f"clean, {warnings} warning(s)" if warnings else "clean"
    if name == "GENERATE_REPORT":
        return "draft report assembled"
    return ""


# ------------------------------------------------------------------ CLI helpers


def load_manifest(manifest_id: str) -> Optional[dict[str, Any]]:
    payload = json.loads((DATA_DIR / "synthetic_manifests.json").read_text(encoding="utf-8"))
    return next((m for m in payload["manifests"] if m["manifest_id"] == manifest_id), None)


def render_report(final_output: dict[str, Any]) -> str:
    """Human-readable rendering of the approved draft report."""
    a = final_output["assessment"]
    n = final_output["narrative"]
    width = 74
    lines = [
        "",
        "=" * width,
        f"  {final_output['document_title']}",
        "=" * width,
        "",
        f"  Manifest : {final_output['manifest_id'] or '(ad hoc)'}",
        f"  Product  : {final_output['product_description']}",
        "",
        "  FINDINGS",
        "  " + "-" * (width - 4),
        f"  HS code applied          : {a['hs_code']}  ({a['hs_code_source']})",
    ]
    if a["rule_description"]:
        lines.append(f"  Rule                     : {a['rule_description']}")
    lines += [
        f"  Total component cost     : USD {a['total_cost_usd']:,.2f}",
        f"  Regionally sourced       : USD {a['regionally_sourced_cost_usd']:,.2f}",
        f"  Non-regionally sourced   : USD {a['non_regionally_sourced_cost_usd']:,.2f}",
    ]
    if a["calculated_rvc_percent"] is not None:
        lines.append(f"  Calculated RVC           : {a['calculated_rvc_percent']:.2f}%")
    if a["rvc_threshold_percent"] is not None:
        lines.append(f"  Applicable threshold     : {a['rvc_threshold_percent']:.2f}%")
        if a["margin_vs_threshold_pp"] is not None:
            lines.append(f"  Margin                   : {a['margin_vs_threshold_pp']:+.2f} pp")
    else:
        lines.append("  Applicable threshold     : none — product not covered by the dataset")
    lines += [
        f"  Status                   : {a['compliance_status']}",
        "",
        f"  Method: {a['calculation_method']}",
        "",
    ]

    # Printed ABOVE the prose, not buried under it. If the narrative model has
    # overstated what this document is, the reader needs to know that before they
    # read the sentence, not after.
    claims = final_output["metadata"].get("claims_flagged_in_prose") or []
    if claims:
        lines += [
            "  !! READ THE NARRATIVE BELOW WITH CARE",
            "  " + "-" * (width - 4),
        ]
        lines += _wrap(
            "The narrative model wrote language asserting legal standing this "
            "assessment does not have. The wording is left visible rather than "
            "silently edited, so you can see exactly what it claimed:",
            width,
        )
        lines += [f"    - {c}" for c in claims]
        lines += _wrap(
            "Disregard those phrasings. This document authorises nothing. The "
            "findings above are unaffected — they are calculated in Python from the "
            "bill of materials, not written by the model.",
            width,
        )
        lines += [""]

    lines += [
        "  SUMMARY",
        "  " + "-" * (width - 4),
    ]
    lines += _wrap(n["summary"], width)
    if n["what_this_means"]:
        lines += ["", "  WHAT THIS MEANS", "  " + "-" * (width - 4)] + _wrap(n["what_this_means"], width)
    if n["recommended_next_steps"]:
        lines += ["", "  RECOMMENDED NEXT STEPS", "  " + "-" * (width - 4)]
        for i, step in enumerate(n["recommended_next_steps"], start=1):
            wrapped = _wrap(step, width, indent=6)
            wrapped[0] = f"  {i}. {wrapped[0].strip()}"
            lines += wrapped
    if n.get("limitations"):
        lines += ["", "  LIMITATIONS OF THIS ASSESSMENT", "  " + "-" * (width - 4)]
        lines += _wrap(n["limitations"], width)

    hr = final_output["human_review"]
    lines += [
        "",
        "  HUMAN REVIEW",
        "  " + "-" * (width - 4),
        f"  Decision : {hr['decision']}",
    ]
    if hr["reviewer_notes"]:
        lines += _wrap(f"Notes: {hr['reviewer_notes']}", width)

    v = final_output["verification"]
    if v["warnings"]:
        lines += ["", "  FLAGGED DURING VERIFICATION", "  " + "-" * (width - 4)]
        for w in v["warnings"]:
            lines += _wrap(f"* {w}", width)

    figures = final_output["metadata"].get("figures_written_by_model_in_prose") or []
    if figures:
        lines += [
            "",
            "  NOTE: the narrative model wrote figures into prose despite being "
            "instructed not to:",
        ]
        lines += [f"    - {f}" for f in figures]
        lines += ["  The findings above are unaffected; they come from state, not prose."]

    lines += ["", "  " + "-" * (width - 4)] + _wrap(final_output["disclaimer"], width) + ["", "=" * width, ""]
    return "\n".join(lines)


def _wrap(text: str, width: int, indent: int = 2) -> list[str]:
    import textwrap

    if not text:
        return []
    pad = " " * indent
    return textwrap.wrap(text, width=width - indent - 2, initial_indent=pad, subsequent_indent=pad) or [pad]


def save_trajectory(state: NavigatorState, tag: str = "") -> Path:
    """Write the full run -- input, every node, tool calls, human decision, output."""
    TRAJECTORY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{state.manifest_id or 'adhoc'}{('-' + tag) if tag else ''}-{stamp}.json"
    path = TRAJECTORY_DIR / name
    path.write_text(
        json.dumps(
            {
                "manifest_id": state.manifest_id,
                "raw_input": state.raw_text,
                "trace": [e.model_dump() for e in state.trace],
                "model_calls": state.model_call_count,
                "final_state": state.model_dump(),
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return path


def _scripted_decision_provider(
    decision: str, edited_hs_code: Optional[str]
) -> DecisionProvider:
    """A non-interactive stand-in for the reviewer, for reproducible log capture.

    This exists so the three demonstration trajectories can be regenerated by
    anyone without sitting at a terminal. It does NOT weaken the checkpoint: the
    review panel is still rendered, the state is still checkpointed to disk
    before the decision, and GENERATE_REPORT is still reachable only through an
    explicit approve. What it changes is *who answers* -- and it writes that fact
    into ``human_notes``, so every trajectory produced this way says so on its
    face rather than passing itself off as a human sign-off.

    The interactive terminal prompt remains the default and the only path a real
    user takes.
    """
    chosen = HumanDecision(decision)
    if chosen is HumanDecision.EDIT and not edited_hs_code:
        raise SystemExit("--decision edit also requires --edit-hs-code")
    if edited_hs_code and edited_hs_code not in load_tariff_db()["rules"]:
        raise SystemExit(
            f"--edit-hs-code {edited_hs_code!r} is not in the controlled dataset; "
            "the reviewer cannot introduce a code the rule table does not contain.\n"
            f"Available: {', '.join(sorted(load_tariff_db()['rules']))}"
        )

    def provider(state: NavigatorState) -> tuple[HumanDecision, str, Optional[str]]:
        note = (
            f"SCRIPTED DECISION ({chosen.value}) supplied by the --decision flag for "
            "non-interactive trajectory capture. This was not a human sign-off."
        )
        if chosen is HumanDecision.EDIT:
            print(f"\n  [scripted] decision: edit -> {edited_hs_code}")
            return chosen, note, edited_hs_code
        print(f"\n  [scripted] decision: {chosen.value}")
        return chosen, note, None

    return provider


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AfCFTA Rules-of-Origin Compliance Navigator — assess a manifest."
    )
    # Not `required=True`: --list must work without naming a manifest. The real
    # requirement is enforced below, after --list has had its chance to exit.
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--manifest", help="Run a bundled synthetic manifest, e.g. M005")
    source.add_argument("--file", help="Path to a text file containing a manifest")
    source.add_argument("--stdin", action="store_true", help="Read the manifest from stdin")
    parser.add_argument("--model", default=LOCAL_MODEL)
    parser.add_argument("--tag", default="", help="Label for the trajectory log filename")
    parser.add_argument("--list", action="store_true", help="List bundled manifest ids and exit")
    parser.add_argument(
        "--decision",
        choices=[d.value for d in HumanDecision],
        help=(
            "Answer the human checkpoint non-interactively, for reproducible log "
            "capture. The panel still renders and the decision is recorded as "
            "scripted, not human. Omit this for the real interactive prompt."
        ),
    )
    parser.add_argument(
        "--edit-hs-code",
        help="HS code the scripted reviewer supplies when --decision edit is used.",
    )
    args = parser.parse_args()

    if args.edit_hs_code and args.decision != HumanDecision.EDIT.value:
        parser.error("--edit-hs-code only applies with --decision edit")

    if args.list:
        payload = json.loads((DATA_DIR / "synthetic_manifests.json").read_text(encoding="utf-8"))
        for m in payload["manifests"]:
            print(f"  {m['manifest_id']}  {m['label']}")
        return 0

    if not (args.manifest or args.file or args.stdin):
        parser.error("one of --manifest, --file or --stdin is required (or use --list)")

    ok, info = health_check()
    if not ok:
        print(f"Cannot reach Ollama: {info}")
        print("Start it with `ollama serve`, then retry.")
        return 1

    if args.manifest:
        manifest = load_manifest(args.manifest)
        if manifest is None:
            print(f"No manifest with id {args.manifest!r}. Use --list to see the ids.")
            return 1
        raw_text, manifest_id = manifest["raw_text"], manifest["manifest_id"]
    elif args.file:
        raw_text, manifest_id = Path(args.file).read_text(encoding="utf-8"), Path(args.file).stem
    else:
        raw_text, manifest_id = sys.stdin.read(), "stdin"

    print("\n" + "=" * 74)
    print("  AfCFTA Rules-of-Origin Compliance Navigator")
    print("  Assessment against a controlled set of AfCFTA rule data.")
    print("  Not a legal determination of origin.")
    print("=" * 74)

    provider: Optional[DecisionProvider] = None
    if args.decision:
        print("\n  NON-INTERACTIVE RUN — the human checkpoint will be answered by the")
        print(f"  --decision flag ({args.decision}), not by a person. The trajectory log")
        print("  records this. Omit --decision for the real interactive prompt.")
        provider = _scripted_decision_provider(args.decision, args.edit_hs_code)

    state = run_assessment(
        raw_text, manifest_id=manifest_id, model=args.model, decision_provider=provider
    )

    if state.final_output:
        print(render_report(state.final_output))

    path = save_trajectory(state, tag=args.tag)
    print(f"  Trajectory log: {path}")
    print(f"  Model calls this run: {state.model_call_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
