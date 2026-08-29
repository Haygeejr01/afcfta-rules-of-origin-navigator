"""Head-to-head evaluation: zero-shot baseline vs the structured workflow.

Grading is identical for both systems, and deliberately generous to the baseline:

  hs_code             exact match after normalising 180632 / 1806.32 to one form
  rvc_percent         within +/-0.5 percentage points of ground truth
  compliance_status   exact match
  fully_correct       all three of the above

The +/-0.5pp tolerance exists only to stop the baseline losing on a rounding
difference. The workflow's arithmetic is exact to two decimal places, so the
tolerance can only ever help the baseline, never the agent.

The agent is evaluated through ``run_pipeline_to_verify``, which stops at VERIFY.
Accuracy therefore measures the automated pipeline alone -- no scripted human
stands in for the reviewer, and the mandatory human checkpoint is neither bypassed
nor credited here. The interrupt is demonstrated separately in the trajectory logs.

Cost: both systems run on local Ollama, so marginal API spend is $0.00. That is
stated rather than presented as an advantage -- the real cost is wall-clock time
and local compute, which is reported in full.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from baseline import run_baseline  # noqa: E402
from graph import run_pipeline_to_verify  # noqa: E402
from llm import LOCAL_MODEL, health_check  # noqa: E402
from nodes import verification_warnings  # noqa: E402
from tools import DATA_DIR  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "logs"

RVC_TOLERANCE_PP = 0.5
COST_PER_CALL_USD = 0.0  # local Ollama: no API charge


def load_manifests() -> list[dict[str, Any]]:
    payload = json.loads((DATA_DIR / "synthetic_manifests.json").read_text(encoding="utf-8"))
    return payload["manifests"]


def _rvc_ok(predicted: Optional[float], expected: float) -> bool:
    return predicted is not None and abs(predicted - expected) <= RVC_TOLERANCE_PP


def _grade(
    hs_code: Optional[str],
    rvc: Optional[float],
    status: Optional[str],
    gt: dict[str, Any],
) -> dict[str, bool]:
    hs_ok = hs_code == gt["expected_hs_code"]
    rvc_ok = _rvc_ok(rvc, gt["expected_rvc_percent"])
    status_ok = status == gt["expected_compliance_status"]
    return {
        "hs_ok": hs_ok,
        "rvc_ok": rvc_ok,
        "status_ok": status_ok,
        "fully_correct": hs_ok and rvc_ok and status_ok,
    }


def _short(hs_code: Optional[str], rvc: Optional[float], status: Optional[str]) -> str:
    code = "UNAVAIL" if hs_code == "CLASSIFICATION_UNAVAILABLE" else (hs_code or "none")
    pct = f"{rvc:.2f}%" if rvc is not None else "n/a"
    return f"{code} / {pct} / {status or 'none'}"


def _notes(
    baseline_grade: dict[str, bool],
    agent_grade: dict[str, bool],
    baseline_row: dict[str, Any],
    agent_state,
    gt: dict[str, Any],
) -> str:
    notes: list[str] = []

    if not baseline_grade["hs_ok"] and gt["expected_hs_code"] == "CLASSIFICATION_UNAVAILABLE":
        notes.append(
            f"baseline invented {baseline_row['hs_code']}, a code that does not cover this product"
        )
    if not baseline_grade["rvc_ok"]:
        pred = baseline_row.get("rvc_percent")
        if pred is not None:
            notes.append(
                f"baseline arithmetic off by {abs(pred - gt['expected_rvc_percent']):.1f}pp"
            )
        else:
            notes.append("baseline produced no percentage")
    if baseline_grade["status_ok"] and not baseline_grade["rvc_ok"]:
        notes.append("baseline verdict right but underlying number wrong")
    if gt["expected_compliance_status"] == "BORDERLINE" and not baseline_grade["status_ok"]:
        notes.append(f"baseline called a borderline case a clean {baseline_row['compliance_status']}")

    if agent_state.hs_code == "CLASSIFICATION_UNAVAILABLE":
        notes.append("agent routed to human review rather than guessing")
    if agent_state.verification_errors:
        notes.append(f"agent VERIFY raised {len(agent_state.verification_errors)} error(s)")
    if not agent_grade["fully_correct"]:
        if not agent_grade["hs_ok"]:
            notes.append(f"agent HS mismatch (got {agent_state.hs_code})")
        if not agent_grade["rvc_ok"]:
            notes.append(f"agent RVC mismatch (got {agent_state.calculated_rvc_percent})")

    return "; ".join(notes) if notes else "both correct"


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare the baseline and the workflow.")
    parser.add_argument("--model", default=LOCAL_MODEL)
    parser.add_argument(
        "--reuse-baseline",
        action="store_true",
        help="Reuse logs/baseline_results.json instead of re-running the baseline",
    )
    parser.add_argument("--out", default=str(LOG_DIR / "comparison_results.json"))
    args = parser.parse_args()

    ok, info = health_check()
    if not ok:
        print(f"Cannot reach Ollama: {info}")
        return 1

    manifests = load_manifests()
    print("=" * 118)
    print("  AfCFTA Compliance Navigator — baseline vs structured workflow")
    print(f"  Model (both systems): {args.model}   |   Cases: {len(manifests)}")
    print(f"  Correct = HS code exact AND RVC within +/-{RVC_TOLERANCE_PP}pp AND status exact")
    print("=" * 118)

    baseline_cache: dict[str, dict[str, Any]] = {}
    if args.reuse_baseline:
        cache_path = LOG_DIR / "baseline_results.json"
        if cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            baseline_cache = {r["manifest_id"]: r for r in cached["results"]}
            print(f"  Reusing cached baseline results from {cache_path}\n")

    rows: list[dict[str, Any]] = []
    for manifest in manifests:
        mid = manifest["manifest_id"]
        gt = manifest["ground_truth"]
        print(f"  {mid} ... ", end="", flush=True)

        # ---- baseline ----
        if mid in baseline_cache:
            baseline_row = baseline_cache[mid]
        else:
            baseline_row = run_baseline(manifest, model=args.model).model_dump()

        b_grade = _grade(
            baseline_row.get("hs_code"),
            baseline_row.get("rvc_percent"),
            baseline_row.get("compliance_status"),
            gt,
        )

        # ---- agent ----
        agent_started = time.perf_counter()
        agent_state = run_pipeline_to_verify(manifest["raw_text"], mid, model=args.model)
        agent_seconds = time.perf_counter() - agent_started

        a_grade = _grade(
            agent_state.hs_code,
            agent_state.calculated_rvc_percent,
            agent_state.compliance_status.value if agent_state.compliance_status else None,
            gt,
        )

        rows.append(
            {
                "manifest_id": mid,
                "label": manifest["label"],
                "ground_truth": _short(
                    gt["expected_hs_code"],
                    gt["expected_rvc_percent"],
                    gt["expected_compliance_status"],
                ),
                "baseline_result": _short(
                    baseline_row.get("hs_code"),
                    baseline_row.get("rvc_percent"),
                    baseline_row.get("compliance_status"),
                ),
                "baseline_correct": "Y" if b_grade["fully_correct"] else "N",
                "baseline_grade": b_grade,
                "baseline_seconds": round(baseline_row.get("duration_ms", 0.0) / 1000.0, 1),
                "baseline_model_calls": baseline_row.get("model_calls", 1),
                "agent_result": _short(
                    agent_state.hs_code,
                    agent_state.calculated_rvc_percent,
                    agent_state.compliance_status.value if agent_state.compliance_status else None,
                ),
                "agent_correct": "Y" if a_grade["fully_correct"] else "N",
                "agent_grade": a_grade,
                "agent_seconds": round(agent_seconds, 1),
                "agent_model_calls": agent_state.model_call_count,
                "agent_verification_errors": agent_state.verification_errors,
                "agent_verification_warnings": verification_warnings(agent_state),
                "notes": _notes(b_grade, a_grade, baseline_row, agent_state, gt),
            }
        )
        print(f"baseline {rows[-1]['baseline_correct']} | agent {rows[-1]['agent_correct']}")

    _print_table(rows)
    summary = _summarise(rows, args.model)
    _print_summary(summary)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out)
    out_path.write_text(
        json.dumps({"summary": summary, "rows": rows}, indent=2, default=str), encoding="utf-8"
    )
    print(f"\n  Full results: {out_path}")
    return 0


def _print_table(rows: list[dict[str, Any]]) -> None:
    print("\n" + "=" * 118)
    header = (
        f"  {'ID':<5} {'BASELINE (hs/rvc/status)':<32} {'OK':<3} "
        f"{'AGENT (hs/rvc/status)':<32} {'OK':<3} NOTES"
    )
    print(header)
    print("  " + "-" * 114)
    for r in rows:
        print(
            f"  {r['manifest_id']:<5} {r['baseline_result']:<32} {r['baseline_correct']:<3} "
            f"{r['agent_result']:<32} {r['agent_correct']:<3} {r['notes'][:34]}"
        )
    print("=" * 118)


def _summarise(rows: list[dict[str, Any]], model: str) -> dict[str, Any]:
    n = len(rows)

    def rate(system: str, key: str) -> int:
        return sum(1 for r in rows if r[f"{system}_grade"][key])

    return {
        "model": model,
        "cases": n,
        "rvc_tolerance_pp": RVC_TOLERANCE_PP,
        "baseline": {
            "hs_correct": rate("baseline", "hs_ok"),
            "rvc_correct": rate("baseline", "rvc_ok"),
            "status_correct": rate("baseline", "status_ok"),
            "fully_correct": rate("baseline", "fully_correct"),
            "avg_seconds": round(sum(r["baseline_seconds"] for r in rows) / n, 1),
            "avg_model_calls": round(sum(r["baseline_model_calls"] for r in rows) / n, 1),
            "total_model_calls": sum(r["baseline_model_calls"] for r in rows),
            "estimated_cost_usd": round(sum(r["baseline_model_calls"] for r in rows) * COST_PER_CALL_USD, 4),
        },
        "agent": {
            "hs_correct": rate("agent", "hs_ok"),
            "rvc_correct": rate("agent", "rvc_ok"),
            "status_correct": rate("agent", "status_ok"),
            "fully_correct": rate("agent", "fully_correct"),
            "avg_seconds": round(sum(r["agent_seconds"] for r in rows) / n, 1),
            "avg_model_calls": round(sum(r["agent_model_calls"] for r in rows) / n, 1),
            "total_model_calls": sum(r["agent_model_calls"] for r in rows),
            "estimated_cost_usd": round(sum(r["agent_model_calls"] for r in rows) * COST_PER_CALL_USD, 4),
        },
        "cost_note": (
            "Both systems run locally through Ollama, so marginal API cost is $0.00. "
            "The real cost is wall-clock latency and local compute, reported above. "
            "The workflow makes more calls per case than the baseline; that is a cost, "
            "not a feature."
        ),
        "human_checkpoint_note": (
            "Accuracy is measured on the automated pipeline up to VERIFY. The mandatory "
            "human approval step is not simulated and not credited here; see "
            "logs/trajectories for runs that exercise it."
        ),
    }


def _print_summary(s: dict[str, Any]) -> None:
    b, a, n = s["baseline"], s["agent"], s["cases"]
    print("\n  SCORECARD")
    print("  " + "-" * 62)
    print(f"  {'dimension':<26} {'baseline':>14} {'workflow':>14}")
    print("  " + "-" * 62)
    for label, key in [
        ("HS code correct", "hs_correct"),
        ("RVC percent correct", "rvc_correct"),
        ("Status correct", "status_correct"),
        ("Fully correct", "fully_correct"),
    ]:
        print(f"  {label:<26} {f'{b[key]}/{n}':>14} {f'{a[key]}/{n}':>14}")
    print("  " + "-" * 62)
    print(f"  {'Avg latency (s)':<26} {b['avg_seconds']:>14} {a['avg_seconds']:>14}")
    print(f"  {'Avg model calls':<26} {b['avg_model_calls']:>14} {a['avg_model_calls']:>14}")
    baseline_cost = f"${b['estimated_cost_usd']:.2f}"
    agent_cost = f"${a['estimated_cost_usd']:.2f}"
    print(f"  {'Estimated cost (USD)':<26} {baseline_cost:>14} {agent_cost:>14}")
    print("  " + "-" * 62)
    print(f"\n  {s['cost_note']}")
    print(f"\n  {s['human_checkpoint_note']}")


if __name__ == "__main__":
    raise SystemExit(main())
