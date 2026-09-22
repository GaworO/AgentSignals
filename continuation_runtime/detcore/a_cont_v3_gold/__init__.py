"""Isolated, correspondence-first A Continuation V3 research detector.

This package is intentionally not imported by the production V2/v31 pipeline.
It contains no exits, targets, P&L, or performance helpers.
"""

from .catalyst_adapter import CatalystAdapter, ConfirmedLocalLiquidity
from .detector import AContinuationV3GoldDetector, CandidateRegistry
from .types import (
    Bar,
    Candidate,
    Catalyst,
    DOLSnapshot,
    Direction,
    EventArray,
    LiquidityPool,
    State,
    Subtype,
)

# Short research-only spelling retained for scratch harness compatibility.
AContV3GoldDetector = AContinuationV3GoldDetector

__all__ = [
    "AContinuationV3GoldDetector",
    "AContV3GoldDetector",
    "CandidateRegistry",
    "CatalystAdapter",
    "ConfirmedLocalLiquidity",
    "Bar",
    "Candidate",
    "Catalyst",
    "DOLSnapshot",
    "Direction",
    "EventArray",
    "LiquidityPool",
    "State",
    "Subtype",
]
