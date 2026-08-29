"""Deterministic tools. No LLM calls, no network, no I/O beyond reading the local
JSON datasets.

Everything in this module is a pure function of its arguments plus two on-disk
datasets that are read once and cached. This is the module that owns every number
the product reports. The LLM is never asked to add, divide, or compare anything.

Why ``Decimal`` and not ``float``
---------------------------------
The product has a 2-percentage-point BORDERLINE band, so verdicts hinge on small
differences. In binary floating point ``4080 / 8000 * 100`` is not exactly 51.0,
and a naive ``>=`` against a 51.0 threshold can flip on the last bit. All sums and
the division run in ``Decimal``, the percentage is quantised to two decimal places
once, and *the quantised value is what gets compared to the threshold*. That means
the number printed on the report and the verdict beside it can never disagree --
a report reading "50.00% against a 50% threshold: FAIL" is not possible.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional

from pydantic import BaseModel, ConfigDict

from state import CLASSIFICATION_UNAVAILABLE, ComplianceStatus

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
TARIFF_DB_PATH = DATA_DIR / "mock_tariff_db.json"
STATE_PARTIES_PATH = DATA_DIR / "afcfta_state_parties.json"

_CENTS = Decimal("0.01")


# --------------------------------------------------------------------------- data


@lru_cache(maxsize=1)
def load_tariff_db() -> dict[str, Any]:
    """Read and cache the mock tariff rule table."""
    with TARIFF_DB_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def load_state_parties() -> dict[str, Any]:
    """Read and cache the eligible-origin list."""
    with STATE_PARTIES_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def borderline_band_pp() -> float:
    """The +/- percentage-point band that makes a result BORDERLINE.

    Read from the dataset rather than hardcoded so the policy can be tuned in one
    place without touching calculation logic.
    """
    return float(load_tariff_db()["metadata"]["borderline_band_pp"])


def hs_code_shortlist() -> list[dict[str, Any]]:
    """Every rule in the controlled dataset, as the shortlist offered to CLASSIFY_HS.

    The whole table is the shortlist. With ten rules there is nothing to retrieve
    -- adding a retrieval step would introduce a way to *hide* the correct answer
    from the model without making the prompt meaningfully shorter.
    """
    db = load_tariff_db()
    return [
        {
            "hs_code": code,
            "description": rule["description"],
            "sector": rule["sector"],
        }
        for code, rule in db["rules"].items()
    ]


# ------------------------------------------------------------------ country match


def _normalise(text: str) -> str:
    """Lowercase, strip accents-free punctuation noise, collapse whitespace."""
    lowered = text.strip().lower().replace("’", "'").replace("`", "'")
    lowered = re.sub(r"[.]", "", lowered)
    return re.sub(r"\s+", " ", lowered)


def _strip_accents(text: str) -> str:
    import unicodedata

    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


class OriginMatch(BaseModel):
    """How an origin string was interpreted.

    Three outcomes, and the distinction matters for reviewer attention:

      eligible  an AfCFTA State Party; its cost counts toward regional content
      external  a recognised trading partner; correctly does not count
      unknown   could not be interpreted at all

    Only ``unknown`` is a review flag. Treating every non-eligible origin as a
    flag buried the real signal under routine imports on the first live run.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str  # "eligible" | "external" | "unknown"
    canonical: Optional[str] = None


@lru_cache(maxsize=1)
def _origin_index() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Build normalised lookup maps once: (eligible, external, aliases)."""
    parties = load_state_parties()
    eligible = {_normalise(_strip_accents(c)): c for c in parties["eligible_origins"]}
    external = {
        _normalise(_strip_accents(c)): c for c in parties.get("known_external_origins", [])
    }
    aliases = {
        _normalise(_strip_accents(k)): v for k, v in parties.get("aliases", {}).items()
    }
    return eligible, external, aliases


@lru_cache(maxsize=512)
def classify_origin(raw_origin: str) -> OriginMatch:
    """Interpret a free-text origin string.

    Handles the shapes an extraction step actually produces: a bare country name,
    an alias ("Ivory Coast", "UAE"), and a city-qualified origin ("Tema, Ghana",
    "tanned Kano Nigeria" -- both observed in live runs).

    An ambiguous string that matches more than one country deliberately resolves to
    ``unknown`` so a human decides, rather than the code picking one.
    """
    eligible, external, aliases = _origin_index()
    probe = _normalise(_strip_accents(raw_origin))
    if not probe:
        return OriginMatch(kind="unknown")

    def _kind_of(canonical: str) -> OriginMatch:
        if canonical in eligible.values():
            return OriginMatch(kind="eligible", canonical=canonical)
        return OriginMatch(kind="external", canonical=canonical)

    # 1. exact canonical name, then alias
    if probe in eligible:
        return OriginMatch(kind="eligible", canonical=eligible[probe])
    if probe in external:
        return OriginMatch(kind="external", canonical=external[probe])
    if probe in aliases:
        return _kind_of(aliases[probe])

    # 2. trailing comma-separated segment, e.g. "Tema, Ghana"
    if "," in probe:
        tail = probe.rsplit(",", 1)[-1].strip()
        if tail in eligible:
            return OriginMatch(kind="eligible", canonical=eligible[tail])
        if tail in external:
            return OriginMatch(kind="external", canonical=external[tail])
        if tail in aliases:
            return _kind_of(aliases[tail])

    # 3. exactly one known country appears as a whole-word phrase in the string,
    #    e.g. "tanned Kano Nigeria". Ambiguous matches fall through to unknown.
    combined = {**external, **eligible, **aliases}
    hits = {
        canonical
        for key, canonical in combined.items()
        if re.search(rf"(?<!\w){re.escape(key)}(?!\w)", probe)
    }
    if len(hits) == 1:
        return _kind_of(next(iter(hits)))

    return OriginMatch(kind="unknown")


def resolve_origin(raw_origin: str) -> Optional[str]:
    """Canonical name if the origin is an eligible State Party, otherwise None.

    None means "does not count toward regional content" and covers both recognised
    imports and uninterpretable strings. Use ``classify_origin`` when the difference
    matters.
    """
    match = classify_origin(raw_origin)
    return match.canonical if match.kind == "eligible" else None


# ------------------------------------------------------------------- LOOKUP_RULE


class RuleLookup(BaseModel):
    """Result of resolving an HS code against the controlled rule table."""

    model_config = ConfigDict(extra="forbid")

    found: bool
    hs_code: Optional[str] = None
    rvc_threshold: Optional[float] = None
    description: str = ""
    sector: str = ""
    reason: str = ""


def LOOKUP_RULE(hs_code: Optional[str]) -> RuleLookup:
    """hs_code -> RVC threshold, strictly from the local dataset.

    A code that is absent from the table yields ``found=False`` and a threshold of
    None. There is no fallback default: a default threshold would let an unknown
    product be silently assessed against a rule that does not govern it.
    """
    if hs_code is None:
        return RuleLookup(found=False, reason="No HS code was supplied.")

    if hs_code == CLASSIFICATION_UNAVAILABLE:
        return RuleLookup(
            found=False,
            hs_code=CLASSIFICATION_UNAVAILABLE,
            reason=(
                "Classification was unavailable, so no rule can be applied from the "
                "controlled dataset."
            ),
        )

    rules = load_tariff_db()["rules"]
    normalised = hs_code.strip()
    rule = rules.get(normalised)
    if rule is None:
        return RuleLookup(
            found=False,
            hs_code=normalised,
            reason=(
                f"HS code {normalised!r} is not present in the controlled rule "
                f"dataset ({len(rules)} rules loaded)."
            ),
        )

    return RuleLookup(
        found=True,
        hs_code=normalised,
        rvc_threshold=float(rule["rvc_threshold"]),
        description=rule["description"],
        sector=rule["sector"],
        reason="Matched a rule in the controlled dataset.",
    )


# ----------------------------------------------------------------- CALCULATE_RVC


class RvcCalculation(BaseModel):
    """Result of the regional-value-content computation."""

    model_config = ConfigDict(extra="forbid")

    total_cost: float
    african_sourced_cost: float
    non_african_sourced_cost: float
    rvc_percent: Optional[float]
    compliance_status: ComplianceStatus
    rvc_threshold: Optional[float] = None
    margin_pp: Optional[float] = None
    unrecognized_origins: list[str] = []
    basis: str = ""


def _component_pairs(
    bill_of_materials: Iterable[Any],
) -> list[tuple[str, str, Decimal]]:
    """Coerce components (Pydantic models *or* plain dicts) to comparable tuples.

    Accepting plain dicts is deliberate: it lets the arithmetic be exercised
    standalone from the dataset with no Pydantic models and no LLM in the loop.
    """
    pairs: list[tuple[str, str, Decimal]] = []
    for item in bill_of_materials:
        if hasattr(item, "component_name"):
            name, origin, cost = item.component_name, item.origin_country, item.cost_usd
        else:
            name = item["component_name"]
            origin = item["origin_country"]
            cost = item["cost_usd"]
        pairs.append((str(name), str(origin), Decimal(str(cost))))
    return pairs


def CALCULATE_RVC(
    bill_of_materials: Iterable[Any],
    rvc_threshold: Optional[float] = None,
    band_pp: Optional[float] = None,
) -> RvcCalculation:
    """Regional value content, build-up method, materials-only cost base.

        RVC% = sum(cost of components from eligible origins) / sum(all component costs) * 100

    Because the cost base is exactly the bill of materials -- no labour, overhead
    or profit -- eligible and non-eligible costs sum to the total. The build-up
    result is therefore algebraically identical to the build-down
    ``(total - non_originating) / total`` form, so the choice of method cannot
    change a verdict here. This is a simplification of real RVC practice, which
    works off ex-works price and would include domestic labour and overhead as
    regional value. Excluding them makes this calculation *conservative*: it
    understates RVC relative to an ex-works basis, so it errs toward review
    rather than toward a false PASS.

    ``rvc_threshold=None`` (an unclassified product) yields NEEDS_HUMAN_REVIEW.
    The percentage is still computed and reported, because it depends only on the
    bill of materials -- but no verdict is asserted against a rule that was never
    identified.
    """
    band = borderline_band_pp() if band_pp is None else band_pp
    pairs = _component_pairs(bill_of_materials)

    total = sum((cost for _, _, cost in pairs), Decimal("0"))
    african = Decimal("0")
    unrecognized: list[str] = []
    for _, origin, cost in pairs:
        match = classify_origin(origin)
        if match.kind == "eligible":
            african += cost
        elif match.kind == "unknown":
            # Still excluded from regional content (the conservative direction),
            # but flagged, because an uninterpretable origin means the percentage
            # may be understated and a human should confirm it.
            unrecognized.append(origin)

    total_q = total.quantize(_CENTS, rounding=ROUND_HALF_UP)
    african_q = african.quantize(_CENTS, rounding=ROUND_HALF_UP)
    non_african_q = (total - african).quantize(_CENTS, rounding=ROUND_HALF_UP)

    if not pairs or total <= 0:
        return RvcCalculation(
            total_cost=float(total_q),
            african_sourced_cost=float(african_q),
            non_african_sourced_cost=float(non_african_q),
            rvc_percent=None,
            compliance_status=ComplianceStatus.NEEDS_HUMAN_REVIEW,
            rvc_threshold=rvc_threshold,
            unrecognized_origins=sorted(set(unrecognized)),
            basis=(
                "No positive total cost was available, so no percentage could be "
                "computed."
            ),
        )

    pct = (african / total * Decimal("100")).quantize(_CENTS, rounding=ROUND_HALF_UP)
    pct_f = float(pct)

    if rvc_threshold is None:
        return RvcCalculation(
            total_cost=float(total_q),
            african_sourced_cost=float(african_q),
            non_african_sourced_cost=float(non_african_q),
            rvc_percent=pct_f,
            compliance_status=ComplianceStatus.NEEDS_HUMAN_REVIEW,
            rvc_threshold=None,
            margin_pp=None,
            unrecognized_origins=sorted(set(unrecognized)),
            basis=(
                "Regional value content computed from the bill of materials, but no "
                "threshold was identified in the controlled dataset, so no "
                "compliance verdict is asserted."
            ),
        )

    threshold_d = Decimal(str(rvc_threshold))
    margin = (pct - threshold_d).quantize(_CENTS, rounding=ROUND_HALF_UP)

    # Borderline is tested first: a result inside the band must not be reported as
    # a clean PASS or FAIL even though it technically sits on one side of the line.
    if abs(margin) <= Decimal(str(band)):
        status = ComplianceStatus.BORDERLINE
        basis = (
            f"{pct}% is within the +/-{band}pp borderline band around the "
            f"{rvc_threshold}% threshold; too close to call without review."
        )
    elif pct >= threshold_d:
        status = ComplianceStatus.PASS
        basis = f"{pct}% meets or exceeds the {rvc_threshold}% threshold."
    else:
        status = ComplianceStatus.FAIL
        basis = f"{pct}% is below the {rvc_threshold}% threshold."

    return RvcCalculation(
        total_cost=float(total_q),
        african_sourced_cost=float(african_q),
        non_african_sourced_cost=float(non_african_q),
        rvc_percent=pct_f,
        compliance_status=status,
        rvc_threshold=rvc_threshold,
        margin_pp=float(margin),
        unrecognized_origins=sorted(set(unrecognized)),
        basis=basis,
    )
