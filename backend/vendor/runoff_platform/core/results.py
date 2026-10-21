"""[CORE-RESULT-01] Result helpers shared by simulation and API layers."""
from __future__ import annotations

from typing import Dict

import pandas as pd


def validate_result(df: pd.DataFrame, grid_type: str) -> Dict[str, float]:
    """Return numerical and water-balance diagnostics without changing results."""
    checks = {
        "max_abs_hourly_balance_error": float(df["water_balance_error"].abs().max()),
        "cumulative_balance_error": float(df["water_balance_error"].sum()),
        "minimum_runoff": float(df["total_runoff"].min()),
        "minimum_swe": float(df["swe"].min()),
    }
    if grid_type.lower() in {"land", "non_glacier"}:
        checks["minimum_tension_storage"] = float(df[["WU", "WL", "WD"]].min().min())
    return checks
