"""[CORE-PARAM-01] Parameter-tree overrides shared by API and file loaders."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import is_dataclass

from .types import LandParams, daily_source_separation_to_hourly, hourly_source_separation_to_daily


class ParameterOverrideError(ValueError):
    pass


def resolve_land_parameter_overrides(overrides, base: LandParams | None = None):
    """[CORE-PARAM-02] Resolve virtual daily source parameters to hourly KI/KG."""
    result = deepcopy(overrides)
    has_total = "K_total_day" in result
    has_fraction = "groundwater_fraction" in result
    if not (has_total or has_fraction):
        return result
    if "KI" in result or "KG" in result:
        raise ParameterOverrideError(
            "K_total_day/groundwater_fraction cannot be combined with direct KI/KG"
        )
    reference = base or LandParams()
    reference_total, reference_fraction = hourly_source_separation_to_daily(
        reference.KI,
        reference.KG,
    )
    total_day = float(result.pop("K_total_day", reference_total))
    groundwater_fraction = float(
        result.pop("groundwater_fraction", reference_fraction)
    )
    try:
        ki_hour, kg_hour = daily_source_separation_to_hourly(
            total_day,
            groundwater_fraction,
        )
    except ValueError as exc:
        raise ParameterOverrideError(str(exc)) from exc
    result["KI"] = ki_hour
    result["KG"] = kg_hour
    result["source_coefficient_time_step_hours"] = 1.0
    return result


def apply_parameter_overrides(target, overrides):
    for key, value in overrides.items():
        if not hasattr(target, key):
            raise ParameterOverrideError(f"unknown parameter: {key}")
        current = getattr(target, key)
        if isinstance(value, dict) and is_dataclass(current):
            apply_parameter_overrides(current, value)
        else:
            setattr(target, key, value)
    return target
