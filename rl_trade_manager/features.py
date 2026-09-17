"""Phase-1.5 observation ordering: 29 values followed by 29 availability flags."""

NAMES = (
    "direction", "unrealized_r", "mfe_r", "mae_r", "fraction_remaining",
    "sl_distance_r", "tp_distance_r", "minutes_since_entry", "session_remaining",
    "htf_direction_relative", "htf_status", "htf_distance_r",
    "execution_direction_relative", "execution_status", "execution_distance_r", "execution_reached",
    "m1_structure_relative", "protected_m1_swing_price", "protected_m1_distance_r",
    "opposite_m1_mss_bos", "fvg_hold_valid", "fvg_boundary_distance_r",
    "return_1_r", "return_3_r", "return_5_r",
    "execution_dol_progress_since_entry_r", "execution_dol_progress_3_r",
    "bars_since_favorable_extreme", "giveback_from_mfe_r",
)


def relative(direction, trade_direction):
    if direction in (None, 0):
        return 0.0
    sign = 1 if direction == "LONG" else -1 if direction == "SHORT" else direction
    return float(sign * trade_direction)


def status_code(status):
    return {"OPEN": 1.0, "DELIVERED": -1.0, "UNCLEAR": 0.0}.get(status, 0.0)
