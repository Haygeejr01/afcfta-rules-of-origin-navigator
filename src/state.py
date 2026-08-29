"""Typed state for the AfCFTA Rules-of-Origin Compliance Navigator.

Every model here sets ``extra="forbid"``: a node that invents a field fails loudly
at the boundary instead of quietly widening the contract. The workflow passes one
``NavigatorState`` from node to node, and each node returns a *new* validated
state rather than mutating in place, so a malformed transition can never be
half-applied.

Money is declared as ``float`` here because that is what the state contract
specifies and what serialises cleanly to JSON. All *arithmetic* on money happens
in ``tools.py`` using ``decimal.Decimal`` and is quantised before it is written
back into this state -- see the module docstring there for why that matters at
the borderline band.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Sentinel returned by CLASSIFY_HS when no rule in the controlled dataset fits the
# product. This is a first-class outcome, not an error: it routes to human review.
# It must never be replaced with a plausible-looking HS code.
CLASSIFICATION_UNAVAILABLE = "CLASSIFICATION_UNAVAILABLE"


class ComplianceStatus(str, Enum):
    """Outcome of the deterministic RVC comparison.

    BORDERLINE exists because a result that clears or misses the threshold by a
    hair is not usefully described as PASS or FAIL -- it is the case where the
    human reviewer's judgement is worth the most.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    BORDERLINE = "BORDERLINE"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"


class HumanDecision(str, Enum):
    """What the reviewer chose at the interrupt."""

    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"


class Component(BaseModel):
    """One line of the bill of materials."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    component_name: str = Field(min_length=1)
    origin_country: str = Field(min_length=1)
    cost_usd: float = Field(ge=0.0)

    @field_validator("cost_usd")
    @classmethod
    def _reject_non_finite(cls, v: float) -> float:
        # A NaN cost would poison every downstream sum while passing a ``>= 0``
        # check, so it is rejected at the edge.
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("cost_usd must be a finite number")
        return v


class TraceEvent(BaseModel):
    """One node execution, captured for the trajectory logs and the eval harness.

    ``model`` is None for deterministic nodes. That is the field the evaluation
    counts to report model calls per run, so it is the single source of truth for
    "how many LLM calls did this system actually make".
    """

    model_config = ConfigDict(extra="forbid")

    node: str
    status: str = "ok"
    model: Optional[str] = None
    duration_ms: float = 0.0
    detail: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )


class NavigatorState(BaseModel):
    """The single state object threaded through the workflow."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # --- input -------------------------------------------------------------
    manifest_id: Optional[str] = None
    raw_text: str = ""

    # --- EXTRACT -----------------------------------------------------------
    product_description: str = ""
    bill_of_materials: list[Component] = Field(default_factory=list)

    # --- CLASSIFY_HS -------------------------------------------------------
    hs_code: Optional[str] = None
    hs_classification_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    hs_classification_rationale: str = ""

    # --- LOOKUP_RULE (deterministic) ---------------------------------------
    # None whenever hs_code is CLASSIFICATION_UNAVAILABLE: there is no rule to
    # compare against, and a default of 0.0 would silently make everything pass.
    rvc_threshold: Optional[float] = None
    rule_description: str = ""

    # --- CALCULATE_RVC (deterministic) -------------------------------------
    total_cost: float = Field(default=0.0, ge=0.0)
    african_sourced_cost: float = Field(default=0.0, ge=0.0)
    non_african_sourced_cost: float = Field(default=0.0, ge=0.0)
    calculated_rvc_percent: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    unrecognized_origins: list[str] = Field(default_factory=list)
    compliance_status: Optional[ComplianceStatus] = None

    # --- VERIFY ------------------------------------------------------------
    verification_errors: list[str] = Field(default_factory=list)

    # --- HUMAN_REVIEW ------------------------------------------------------
    human_decision: Optional[HumanDecision] = None
    human_notes: str = ""

    # --- GENERATE_REPORT ---------------------------------------------------
    final_output: Optional[dict[str, Any]] = None

    # --- observability -----------------------------------------------------
    trace: list[TraceEvent] = Field(default_factory=list)

    # ------------------------------------------------------------------ utils
    def with_updates(self, **updates: Any) -> "NavigatorState":
        """Return a new validated state with ``updates`` applied.

        Nodes use this instead of mutating so that a validation failure leaves the
        previous state intact and inspectable.
        """
        return NavigatorState.model_validate({**self.model_dump(), **updates})

    def logged(self, event: TraceEvent) -> "NavigatorState":
        """Return a new state with ``event`` appended to the trace."""
        return self.model_copy(update={"trace": [*self.trace, event]}, deep=True)

    @property
    def model_call_count(self) -> int:
        """Number of LLM calls actually made, read straight off the trace."""
        return sum(1 for e in self.trace if e.model is not None)

    @property
    def is_classified(self) -> bool:
        return self.hs_code is not None and self.hs_code != CLASSIFICATION_UNAVAILABLE
