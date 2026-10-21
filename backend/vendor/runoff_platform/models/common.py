"""Common meteorological processes used by glacier and land grids."""
from __future__ import annotations

import pandas as pd

from ..core.types import NUMERICAL, PHYSICAL, PETParams, PhaseParams


# [COMMON-01] Precipitation phase partition
def partition_precip(precipitation: float, temperature: float, params: PhaseParams):
    precipitation = max(float(precipitation), 0.0)
    if temperature <= params.all_snow_temp:
        snow_fraction = 1.0
    elif temperature >= params.all_rain_temp:
        snow_fraction = 0.0
    else:
        interval = params.all_rain_temp - params.all_snow_temp
        if interval <= NUMERICAL.epsilon:
            raise ValueError("all_rain_temp must be greater than all_snow_temp")
        snow_fraction = (params.all_rain_temp - temperature) / interval
    return (
        precipitation * (1.0 - snow_fraction),
        precipitation * snow_fraction,
        snow_fraction,
    )


# [COMMON-02] Degree-day or simplified energy-balance melt potential
def potential_melt(temperature, degree_day_factor, melt_temp, row, method, default_albedo):
    if method == "degree_day":
        return degree_day_factor * max(float(temperature) - melt_temp, 0.0) / PHYSICAL.hours_per_day
    if method != "energy_balance":
        raise ValueError(f"unsupported melt method: {method}")
    shortwave = max(float(row.get("shortwave", 0.0)), 0.0)
    energy = (
        (1.0 - float(row.get("albedo", default_albedo))) * shortwave
        + float(row.get("longwave_net", 0.0))
        + float(row.get("sensible_heat", 0.0))
        + float(row.get("latent_heat", 0.0))
        + float(row.get("rain_heat", 0.0))
    )
    return (
        max(energy, 0.0)
        * PHYSICAL.seconds_per_hour
        / (PHYSICAL.water_density_kg_m3 * PHYSICAL.latent_heat_fusion_j_kg)
        * PHYSICAL.millimeters_per_meter
    )


# [COMMON-03] Hourly PET input or fallback estimate
def pet_hourly(row, params: PETParams) -> float:
    if "pet" in row and pd.notna(row["pet"]):
        return max(float(row["pet"]), 0.0)
    temperature = float(row["temperature"])
    shortwave = max(float(row.get("shortwave", 0.0)), 0.0)
    return min(
        params.maximum_hourly_pet_mm,
        max(
            0.0,
            params.shortwave_coefficient * shortwave
            + params.positive_temperature_coefficient * max(temperature, 0.0),
        ),
    )
