from dataclasses import dataclass


@dataclass(frozen=True)
class Bar:
    ms: int  # minute OPEN timestamp, UTC
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Trade:
    key: str
    sample_index: int
    direction: int
    entry: float
    initial_sl: float
    target: float
    qty: int
    risk: float
    fill_ms: int
    exit_ms: int
    exit_reason: str
    canonical_r: float
    session: str
    strategy_id: str
    htf_direction: int
    htf_price: float | None
    htf_status_at_entry: str | None
    execution_direction: int
    execution_price: float | None
    bars: tuple[Bar, ...]
    pre_bars: tuple[Bar, ...] = ()
    fvg_low: float | None = None
    fvg_high: float | None = None
    dol_states: tuple[dict, ...] = ()
    fvg_signal_ms: int | None = None
    fvg_ce: float | None = None
