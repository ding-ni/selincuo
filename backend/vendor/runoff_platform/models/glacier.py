"""Hourly glacier-grid runoff model."""
from __future__ import annotations

import pandas as pd

from ..core.types import GlacierParams, GlacierState, GridInfo, NUMERICAL, PHYSICAL, RunContext
from ..core.validation import validate_forcing, validate_glacier_state, validate_grid
from .common import partition_precip, pet_hourly, potential_melt
from .snow import snow_step


# [GLACIER-01] Glacier-grid simulation entry
def simulate_glacier(meteo: pd.DataFrame, params=None, state=None, grid=None, context=None) -> pd.DataFrame:
    params = params or GlacierParams()
    state = state or GlacierState()
    grid = grid or GridInfo(zone_type="GLACIER")
    context = context or RunContext()
    if grid.zone_type != "GLACIER":
        raise ValueError("simulate_glacier requires grid.zone_type='GLACIER'")
    if context.time_step_hours != NUMERICAL.supported_time_step_hours:
        raise ValueError("the current model supports only a 1-hour time step")
    validate_forcing(meteo)
    validate_grid(grid)
    validate_glacier_state(state)
    rows = []

    for time, row in meteo.iterrows():
        # [GLACIER-01A] Initial active storage and forcing
        old_storage = state.swe + state.snow_liquid + state.glacier_storage
        precipitation = max(float(row.precipitation), 0.0)
        temperature = float(row.temperature)
        pet = pet_hourly(row, params.pet)

        # [GLACIER-02] Rain path depends on whether snow is present
        rainfall, snowfall, snow_fraction = partition_precip(precipitation, temperature, params.phase)
        snow_present = state.swe > NUMERICAL.epsilon or snowfall > NUMERICAL.epsilon
        rain_on_snow = rainfall if snow_present else 0.0
        rain_on_bare_ice = rainfall - rain_on_snow
        (
            state.swe, state.snow_liquid, snowmelt, refreeze, snow_sublimation,
            snow_drainage, pet_left, bare_ice_fraction,
        ) = snow_step(
            state.swe, state.snow_liquid, snowfall, rain_on_snow,
            temperature, pet, params.snow, row,
        )

        # [GLACIER-03] Bare-ice melt uses only the remaining fraction of the hour
        icemelt = 0.0
        ice_sublimation = 0.0
        if state.swe <= NUMERICAL.epsilon:
            if params.snow.melt_method == "degree_day":
                full_hour_melt = params.ice_ddf * max(temperature - params.snow.melt_temp, 0.0) / PHYSICAL.hours_per_day
            else:
                full_hour_melt = potential_melt(
                    temperature, params.ice_ddf, params.snow.melt_temp,
                    row, "energy_balance", params.snow.default_albedo,
                )
            icemelt = full_hour_melt * bare_ice_fraction
            ice_sublimation = min(
                pet_left * params.ice_sublimation_fraction_pet,
                params.ice_sublimation_max_mm_h,
            ) * bare_ice_fraction

        # [GLACIER-04] Temporary glacier reservoir
        reservoir_inflow = snow_drainage + rain_on_bare_ice + icemelt
        state.glacier_storage += reservoir_inflow
        glacier_runoff = min(state.glacier_storage, params.reservoir_k * state.glacier_storage)
        state.glacier_storage -= glacier_runoff

        # [GLACIER-05] Water balance: ice melt/sublimation are long-term ice sources
        new_storage = state.swe + state.snow_liquid + state.glacier_storage
        balance_error = (
            precipitation + icemelt + ice_sublimation
            - glacier_runoff - snow_sublimation - ice_sublimation
            - (new_storage - old_storage)
        )
        rows.append({
            "time": time, "run_id": context.run_id, "scenario_id": context.scenario_id,
            "param_set_id": context.param_set_id, "unit_id": grid.unit_id,
            "zone_type": grid.zone_type, "area_km2": grid.area_km2,
            "elevation_m": grid.elevation_m, "slope_degree": grid.slope_degree,
            "precipitation": precipitation, "temperature": temperature,
            "rainfall": rainfall, "snowfall": snowfall, "snow_fraction": snow_fraction,
            "snowmelt": snowmelt, "icemelt": icemelt,
            "bare_ice_fraction": bare_ice_fraction, "refreeze": refreeze,
            "snow_sublimation": snow_sublimation, "ice_sublimation": ice_sublimation,
            "sublimation": snow_sublimation + ice_sublimation,
            "glacier_ablation": icemelt + ice_sublimation,
            "snow_drainage": snow_drainage, "glacier_runoff": glacier_runoff,
            "infiltration_excess_runoff": 0.0, "saturation_excess_runoff": 0.0,
            "surface_runoff": 0.0, "RS": 0.0, "interflow": 0.0,
            "RI": 0.0, "baseflow": 0.0, "RG": 0.0,
            "runoff_mechanism": "GLACIER", "total_runoff": glacier_runoff,
            "et_actual": snow_sublimation + ice_sublimation,
            "swe": state.swe, "snow_liquid": state.snow_liquid,
            "glacier_storage": state.glacier_storage,
            "soil1": float("nan"), "soil2": float("nan"),
            "soil3": float("nan"), "groundwater": float("nan"),
            "water_balance_error": balance_error,
            "quality_flag": "OK" if abs(balance_error) < NUMERICAL.balance_tolerance_mm else "BALANCE_WARNING",
        })
    return pd.DataFrame(rows).set_index("time")
