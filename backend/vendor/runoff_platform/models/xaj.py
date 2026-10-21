"""Three-source Xin'anjiang runoff generation for a non-glacier grid."""
from __future__ import annotations

import pandas as pd

from ..core.types import GridInfo, LandParams, LandState, NUMERICAL, RunContext
from ..core.validation import validate_forcing, validate_grid, validate_land_params, validate_land_state
from .common import partition_precip, pet_hourly
from .conversion import saturation_infiltration_conversion
from .snow import snow_step


# [XAJ-01] Three-layer evapotranspiration
def xaj_evaporation(wu, wl, wd, liquid_input, pet, params: LandParams):
    rainfall = max(float(liquid_input), 0.0)
    demand = max(float(pet), 0.0)
    available_upper = rainfall + wu
    eu = min(available_upper, demand)
    rain_used = min(rainfall, eu)
    rainfall -= rain_used
    wu -= eu - rain_used
    remaining = demand - eu
    if remaining <= NUMERICAL.epsilon:
        return wu, wl, wd, rainfall, eu, 0.0, 0.0
    if wl >= params.C * params.WLM:
        el = min(wl, remaining * wl / max(params.WLM, NUMERICAL.epsilon))
        ed = 0.0
    elif wl >= params.C * remaining:
        el = min(wl, params.C * remaining)
        ed = 0.0
    else:
        el = wl
        ed = min(wd, max(params.C * remaining - el, 0.0))
    return wu, wl - el, wd - ed, rainfall, eu, el, ed


# [XAJ-02] Tension-water capacity curve and saturation-excess generation
def xaj_runoff_generation(effective_rain: float, tension_storage: float, params: LandParams):
    pe = max(float(effective_rain), 0.0)
    w = min(max(float(tension_storage), 0.0), params.WM)
    if pe <= NUMERICAL.epsilon:
        return 0.0, w
    wmm = params.WM * (1.0 + params.B)
    a = wmm * (1.0 - (1.0 - w / params.WM) ** (1.0 / (1.0 + params.B)))
    if pe + a < wmm:
        runoff = pe - (params.WM - w) + params.WM * (1.0 - (pe + a) / wmm) ** (1.0 + params.B)
    else:
        runoff = pe - (params.WM - w)
    runoff = min(max(runoff, 0.0), pe)
    return runoff, min(params.WM, w + pe - runoff)


# [XAJ-03] Store retained rainfall in upper, lower and deep layers
def fill_tension_storage(wu, wl, wd, addition, params: LandParams):
    addition = max(float(addition), 0.0)
    upper = min(max(params.WUM - wu, 0.0), addition)
    wu += upper
    addition -= upper
    lower = min(max(params.WLM - wl, 0.0), addition)
    wl += lower
    addition -= lower
    deep = min(max(params.WDM - wd, 0.0), addition)
    wd += deep
    return wu, wl, wd


# [XAJ-04] Free-water reservoir and RS/RI/RG source separation
def xaj_source_separation(runoff, effective_rain, s, old_fr, params: LandParams):
    old_fr = min(max(float(old_fr), 0.0), 1.0)
    old_volume = max(float(s), 0.0) * old_fr
    if runoff <= NUMERICAL.epsilon:
        ri = params.KI * max(s, 0.0) * old_fr
        rg = params.KG * max(s, 0.0) * old_fr
        return 0.0, ri, rg, max(s * (1.0 - params.KI - params.KG), 0.0), old_fr

    fr = min(max(runoff / max(effective_rain, NUMERICAL.epsilon), NUMERICAL.epsilon), 1.0)
    overflow = max(old_volume - params.SM * fr, 0.0)
    s = min(old_volume / fr, params.SM)
    smm = params.SM * (1.0 + params.EX)
    relative = min(max(s / params.SM, 0.0), 1.0)
    au = smm * (1.0 - (1.0 - relative) ** (1.0 / (1.0 + params.EX)))
    if effective_rain + au < smm:
        rs_curve = fr * (
            effective_rain - params.SM + s
            + params.SM * (1.0 - (effective_rain + au) / smm) ** (1.0 + params.EX)
        )
    else:
        rs_curve = fr * (effective_rain + s - params.SM)
    rs_curve = min(max(rs_curve, 0.0), runoff)
    rs = overflow + rs_curve
    s = min(params.SM, max(0.0, s + (runoff - rs_curve) / fr))
    ri = s * params.KI * fr
    rg = s * params.KG * fr
    s = max(s * (1.0 - params.KI - params.KG), 0.0)
    return rs, ri, rg, s, fr


# [LAND-XAJ-01] Hourly non-glacier simulation entry
def simulate_land(meteo: pd.DataFrame, params=None, state=None, grid=None, context=None) -> pd.DataFrame:
    params = params or LandParams()
    state = state or LandState()
    grid = grid or GridInfo(zone_type="NON_GLACIER")
    context = context or RunContext()
    if grid.zone_type != "NON_GLACIER":
        raise ValueError("simulate_land requires grid.zone_type='NON_GLACIER'")
    if context.time_step_hours != NUMERICAL.supported_time_step_hours:
        raise ValueError("the current model supports only a 1-hour time step")
    validate_forcing(meteo)
    validate_grid(grid)
    validate_land_params(params)
    validate_land_state(state, params)
    rows = []

    for time, row in meteo.iterrows():
        # [LAND-XAJ-01A] Initial active storage
        old_free = state.S * state.FR
        old_storage = state.swe + state.snow_liquid + state.WU + state.WL + state.WD + old_free
        precipitation = max(float(row.precipitation), 0.0)
        temperature = float(row.temperature)
        pet = pet_hourly(row, params.pet) * params.KC

        # [LAND-XAJ-01B] Rain/snow partition and seasonal snow process
        rainfall, snowfall, snow_fraction = partition_precip(precipitation, temperature, params.phase)
        snow_present = state.swe > NUMERICAL.epsilon or snowfall > NUMERICAL.epsilon
        rain_on_snow = rainfall if snow_present else 0.0
        (
            state.swe, state.snow_liquid, snowmelt, refreeze, snow_sublimation,
            snow_drainage, pet_left, _,
        ) = snow_step(
            state.swe, state.snow_liquid, snowfall, rain_on_snow,
            temperature, pet, params.snow, row,
        )

        # [LAND-XAJ-01C] Three-layer ET and net rainfall
        liquid_input = rainfall - rain_on_snow + snow_drainage
        state.WU, state.WL, state.WD, net_rainfall, eu, el, ed = xaj_evaporation(
            state.WU, state.WL, state.WD, liquid_input, pet_left, params
        )
        et_actual = snow_sublimation + eu + el + ed

        # [LAND-XAJ-01D] Optional conversion, then standard XAJ generation
        conversion_rs, xaj_input, activation, capacity = saturation_infiltration_conversion(
            net_rainfall, grid.slope_degree, params.conversion
        )
        w_before = state.WU + state.WL + state.WD
        pervious_input = xaj_input * (1.0 - params.IM)
        pervious_runoff, w_after = xaj_runoff_generation(pervious_input, w_before, params)
        impervious_runoff = xaj_input * params.IM
        state.WU, state.WL, state.WD = fill_tension_storage(
            state.WU, state.WL, state.WD, max(w_after - w_before, 0.0), params
        )

        # [LAND-XAJ-01E] Three-source separation
        saturation_rs, ri, rg, state.S, state.FR = xaj_source_separation(
            pervious_runoff, max(pervious_input, NUMERICAL.epsilon), state.S, state.FR, params
        )
        rs = conversion_rs + impervious_runoff + saturation_rs
        total_runoff = rs + ri + rg

        # [LAND-XAJ-01F] Water balance and platform output
        new_free = state.S * state.FR
        new_storage = state.swe + state.snow_liquid + state.WU + state.WL + state.WD + new_free
        balance_error = precipitation - total_runoff - et_actual - (new_storage - old_storage)
        rows.append({
            "time": time, "run_id": context.run_id, "scenario_id": context.scenario_id,
            "param_set_id": context.param_set_id, "unit_id": grid.unit_id,
            "zone_type": grid.zone_type, "area_km2": grid.area_km2,
            "elevation_m": grid.elevation_m, "slope_degree": grid.slope_degree,
            "precipitation": precipitation, "temperature": temperature,
            "rainfall": rainfall, "snowfall": snowfall, "snow_fraction": snow_fraction,
            "snowmelt": snowmelt, "icemelt": 0.0, "bare_ice_fraction": 0.0,
            "refreeze": refreeze, "snow_sublimation": snow_sublimation,
            "ice_sublimation": 0.0, "sublimation": snow_sublimation,
            "glacier_ablation": 0.0, "snow_drainage": snow_drainage,
            "runoff_mechanism": "XAJ_WITH_CONVERSION" if params.conversion.enabled else "XAJ_SATURATION_EXCESS",
            "conversion_enabled": params.conversion.enabled,
            "conversion_activation": activation,
            "effective_infiltration_capacity": capacity,
            "infiltration_excess_RS": conversion_rs,
            "saturation_excess_RS": saturation_rs + impervious_runoff,
            "impervious_runoff": impervious_runoff,
            "pervious_runoff": pervious_runoff,
            "net_rainfall": net_rainfall, "xaj_input": xaj_input,
            "RS": rs, "RI": ri, "RG": rg, "surface_runoff": rs,
            "interflow": ri, "baseflow": rg, "glacier_runoff": 0.0,
            "total_runoff": total_runoff, "et_actual": et_actual,
            "EU": eu, "EL": el, "ED": ed,
            "swe": state.swe, "snow_liquid": state.snow_liquid,
            "WU": state.WU, "WL": state.WL, "WD": state.WD,
            "W": state.WU + state.WL + state.WD, "S": state.S, "FR": state.FR,
            "free_water_storage": new_free, "water_balance_error": balance_error,
            "quality_flag": "OK" if abs(balance_error) < NUMERICAL.balance_tolerance_mm else "BALANCE_WARNING",
        })
    return pd.DataFrame(rows).set_index("time")


simulate_xaj = simulate_land
