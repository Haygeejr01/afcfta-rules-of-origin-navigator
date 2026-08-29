"""Baseline system: one zero-shot LLM call, no tools, no schema, no deterministic math.

This is the honest control. It exists to answer "does the structured workflow
actually earn its complexity?" -- and it is set up so that it *can* win.

Fairness decisions, stated plainly because they determine whether the comparison
means anything:

1. The rule table is pasted into the prompt. The RVC thresholds in this project are
   synthetic, so a model relying on parametric knowledge could never recover them.
   Withholding the table would guarantee a baseline failure on every threshold and
   manufacture a victory for the agent. Handing it over keeps the comparison about
   what the architecture adds -- deterministic arithmetic, constrained
   classification, a human checkpoint -- rather than about who memorised an
   invented number.

2. The baseline is asked to end with three labelled lines. That is a formatting
   request in prose, not a schema: there is no validation, no grammar constraint,
   and no retry. It exists so the output can be graded at all, and it helps the
   baseline rather than hindering it.

3. Temperature is 0, same as the agent, so runs are comparable.

What the baseline is NOT given: a calculator, a lookup function, output validation,
or a human review step. Those are exactly the variables under test.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

sys.path.insert(0, str(Path(__file__).resolve().parent))

from llm import LOCAL_MODEL, call_model  # noqa: E402
from tools import DATA_DIR, load_tariff_db  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "logs"

SYSTEM_PROMPT = (
    "You are a trade compliance analyst assessing whether a product qualifies for "
    "preferential tariff treatment under the AfCFTA rules of origin."
)


def _rule_table_text() -> str:
    db = load_tariff_db()
    lines = [
        f"  {code}  (RVC threshold {rule['rvc_threshold']}%)  {rule['description']}"
        for code, rule in db["rules"].items()
    ]
    return "\n".join(lines)


def _eligible_origins_text() -> str:
    parties = json.loads((DATA_DIR / "afcfta_state_parties.json").read_text(encoding="utf-8"))
    return ", ".join(parties["eligible_origins"])


def build_prompt(raw_text: str) -> str:
    return f"""Assess the following export manifest for AfCFTA rules-of-origin eligibility.

Regional Value Content (RVC) is the share of total component cost that comes from
components originating in an AfCFTA State Party. The product qualifies if its RVC
percentage meets the threshold for its HS code.

Countries that count as regionally sourced:
{_eligible_origins_text()}

The applicable rules, with their RVC thresholds:
{_rule_table_text()}

If no rule above fits the product, say so rather than choosing the closest one.

--- MANIFEST ---
{raw_text}
--- END MANIFEST ---

Work out the HS code, the total component cost, the regionally sourced cost, the
RVC percentage, and whether the product qualifies.

End your reply with exactly these three lines:
HS_CODE: <the six-digit code, or CLASSIFICATION_UNAVAILABLE>
RVC_PERCENT: <the percentage as a number, to two decimal places>
VERDICT: <PASS or FAIL or BORDERLINE or NEEDS_HUMAN_REVIEW>
"""


class BaselineResult(BaseModel):
    """One baseline run, with everything needed to grade it and to show the raw trace."""

    model_config = ConfigDict(extra="forbid")

    manifest_id: str
    model: str
    ok: bool
    error: str = ""
    raw_response: str = ""
    duration_ms: float = 0.0
    model_calls: int = 1
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None

    hs_code: Optional[str] = None
    rvc_percent: Optional[float] = None
    compliance_status: Optional[str] = None
    parse_notes: list[str] = []


_STATUSES = ("NEEDS_HUMAN_REVIEW", "BORDERLINE", "PASS", "FAIL")


def parse_baseline_response(text: str) -> tuple[Optional[str], Optional[float], Optional[str], list[str]]:
    """Pull the three graded fields out of free-form text.

    Generous on purpose: the labelled lines are tried first, then a looser search
    over the whole reply. A field that genuinely cannot be found stays None and is
    noted, because "the baseline produced something ungradeable" is a real result
    worth reporting rather than papering over.
    """
    notes: list[str] = []

    hs_match = re.search(
        r"HS[_\s]?CODE\s*[:=]\s*\*{0,2}\s*(CLASSIFICATION_UNAVAILABLE|\d{4}\.?\d{2})",
        text,
        re.IGNORECASE,
    )
    hs_code: Optional[str] = None
    if hs_match:
        raw = hs_match.group(1).upper()
        hs_code = raw if raw == "CLASSIFICATION_UNAVAILABLE" else _normalise_hs(raw)
    else:
        loose = re.findall(r"\b(\d{4}\.\d{2})\b", text)
        if "CLASSIFICATION_UNAVAILABLE" in text.upper():
            hs_code = "CLASSIFICATION_UNAVAILABLE"
            notes.append("HS code recovered from prose, not the labelled line")
        elif loose:
            hs_code = _normalise_hs(loose[-1])
            notes.append("HS code recovered from prose, not the labelled line")
        else:
            notes.append("no HS code found in response")

    pct_match = re.search(r"RVC[_\s]?PERCENT\s*[:=]\s*\*{0,2}\s*(-?\d+(?:\.\d+)?)", text, re.IGNORECASE)
    rvc: Optional[float] = None
    if pct_match:
        rvc = float(pct_match.group(1))
    else:
        loose_pct = re.findall(r"(-?\d+(?:\.\d+)?)\s*%", text)
        if loose_pct:
            rvc = float(loose_pct[-1])
            notes.append("RVC percent recovered from prose, not the labelled line")
        else:
            notes.append("no RVC percentage found in response")

    verdict_match = re.search(r"VERDICT\s*[:=]\s*\*{0,2}\s*([A-Z_]+)", text, re.IGNORECASE)
    status: Optional[str] = None
    if verdict_match:
        candidate = verdict_match.group(1).upper()
        status = next((s for s in _STATUSES if s == candidate), None)
        if status is None:
            status = next((s for s in _STATUSES if s in candidate), None)
    if status is None:
        tail = text.upper()[-400:]
        status = next((s for s in _STATUSES if s in tail), None)
        if status:
            notes.append("verdict recovered from prose, not the labelled line")
        else:
            notes.append("no verdict found in response")

    return hs_code, rvc, status, notes


def _normalise_hs(code: str) -> str:
    """``180632`` and ``1806.32`` both mean the same rule; compare them as one."""
    digits = re.sub(r"\D", "", code)
    return f"{digits[:4]}.{digits[4:6]}" if len(digits) >= 6 else code


def run_baseline(manifest: dict[str, Any], model: str = LOCAL_MODEL) -> BaselineResult:
    """One manifest through one prompt."""
    prompt = build_prompt(manifest["raw_text"])
    result = call_model(prompt, model=model, system=SYSTEM_PROMPT, temperature=0.0)

    if not result.ok:
        return BaselineResult(
            manifest_id=manifest["manifest_id"],
            model=model,
            ok=False,
            error=result.error,
            duration_ms=result.duration_ms,
            parse_notes=["model call failed"],
        )

    hs_code, rvc, status, notes = parse_baseline_response(result.text)
    return BaselineResult(
        manifest_id=manifest["manifest_id"],
        model=model,
        ok=True,
        raw_response=result.text,
        duration_ms=result.duration_ms,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        hs_code=hs_code,
        rvc_percent=rvc,
        compliance_status=status,
        parse_notes=notes,
    )


def load_manifests() -> list[dict[str, Any]]:
    payload = json.loads((DATA_DIR / "synthetic_manifests.json").read_text(encoding="utf-8"))
    return payload["manifests"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the zero-shot baseline on the manifests.")
    parser.add_argument("--model", default=LOCAL_MODEL)
    parser.add_argument("--manifest", help="Run a single manifest id, e.g. M005")
    parser.add_argument("--out", default=str(LOG_DIR / "baseline_results.json"))
    args = parser.parse_args()

    manifests = load_manifests()
    if args.manifest:
        manifests = [m for m in manifests if m["manifest_id"] == args.manifest]
        if not manifests:
            print(f"No manifest with id {args.manifest!r}")
            return 1

    print(f"Baseline: {args.model} | {len(manifests)} manifest(s) | zero-shot, no tools\n")

    results: list[BaselineResult] = []
    wall_start = time.perf_counter()
    for manifest in manifests:
        gt = manifest["ground_truth"]
        print(f"  {manifest['manifest_id']} ... ", end="", flush=True)
        result = run_baseline(manifest, model=args.model)
        results.append(result)
        if not result.ok:
            print(f"CALL FAILED ({result.error})")
            continue
        correct = (
            result.hs_code == gt["expected_hs_code"]
            and result.compliance_status == gt["expected_compliance_status"]
            and result.rvc_percent is not None
            and abs(result.rvc_percent - gt["expected_rvc_percent"]) < 0.51
        )
        print(
            f"{result.hs_code} | {result.rvc_percent}% | {result.compliance_status} "
            f"| {'OK' if correct else 'MISMATCH'} | {result.duration_ms / 1000:.1f}s"
        )

    total_wall = time.perf_counter() - wall_start
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out)
    out_path.write_text(
        json.dumps(
            {
                "system": "baseline_zero_shot",
                "model": args.model,
                "total_wall_seconds": round(total_wall, 2),
                "results": [r.model_dump() for r in results],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {out_path}")
    print(f"Total wall clock: {total_wall:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
