"""[XAJ-WARMUP-CORE] Vectorized hourly XAJ calculation for liquid precipitation."""

from __future__ import annotations

from typing import Mapping

import numpy as np

from ..core.types import LandParams, NUMERICAL
from ..core.numeric_compilation import optional_njit


LAND_STATE_FIELDS = ("swe", "snow_liquid", "WU", "WL", "WD", "S", "FR")


def _copy_and_validate_state(
    state: Mapping[str, np.ndarray],
    params: LandParams,
    grid_count: int,
) -> dict[str, np.ndarray]:
    missing = [name for name in LAND_STATE_FIELDS if name not in state]
    if missing:
        raise ValueError(f"initial land state is missing fields: {missing}")
    result = {
        name: np.asarray(state[name], dtype=float).copy()
        for name in LAND_STATE_FIELDS
    }
    wrong_size = [name for name, values in result.items() if values.shape != (grid_count,)]
    if wrong_size:
        raise ValueError(
            f"initial land state fields must have shape ({grid_count},): {wrong_size}"
        )
    limits = {
        "swe": (0.0, np.inf),
        "snow_liquid": (0.0, np.inf),
        "WU": (0.0, params.WUM),
        "WL": (0.0, params.WLM),
        "WD": (0.0, params.WDM),
        "S": (0.0, params.SM),
        "FR": (0.0, 1.0),
    }
    # [XAJ-STATE-HANDOFF-20261011] Normalize roundoff only, never real overflow.
    # The absolute tolerance is in mm (dimensionless for FR), shared with
    # numerical settings; no capacity-dependent allowance hides invalid states.
    tolerance = NUMERICAL.epsilon
    invalid = [
        name
        for name, values in result.items()
        if not np.isfinite(values).all()
        or np.any(values < limits[name][0] - tolerance)
        or np.any(values > limits[name][1] + tolerance)
    ]
    if invalid:
        raise ValueError(f"initial land state is outside parameter bounds: {invalid}")
    for name, values in result.items():
        result[name] = np.clip(values, *limits[name])
    return result


# [XAJ-WARMUP-CORE-01] Shared non-glacier liquid-rain XAJ implementation.
def simulate_xaj_liquid_grids(
    precipitation_mm: np.ndarray,
    params: LandParams,
    pet_mm_h: float | np.ndarray,
    initial_state: Mapping[str, np.ndarray],
    return_diagnostics: bool = False,
):
    """Simulate all grids and return RS, RI, RG and the final state.

    The function intentionally requires an explicit initial state. This keeps
    platform configuration outside the XAJ equations and makes warm-up runs
    reproducible. For this non-glacier application precipitation is already
    liquid, so temperature and phase partition are not inputs here.
    """
    precip = np.asarray(precipitation_mm, dtype=float)
    if precip.ndim != 2:
        raise ValueError("precipitation_mm must have shape [time, grid]")
    if not np.isfinite(precip).all() or np.any(precip < 0.0):
        raise ValueError("precipitation_mm must contain finite non-negative values")
    if params.WM <= 0.0 or params.WDM < 0.0 or params.KI + params.KG > 1.0:
        raise ValueError("invalid XAJ storage or source-separation parameters")
    if params.source_coefficient_time_step_hours != 1.0:
        raise ValueError("KI and KG must be coefficients for the 1-hour model time step")
    if params.conversion.enabled:
        raise ValueError(
            "vectorized warm-up requires conversion.enabled=false because the "
            "current batch forcing does not include per-grid slope conversion inputs"
        )

    n_grids = precip.shape[1]
    pet = np.asarray(pet_mm_h, dtype=float)
    if pet.ndim == 0:
        pet = np.full(precip.shape, float(pet), dtype=float)
    elif pet.ndim == 1 and pet.shape == (precip.shape[0],):
        pet = np.repeat(pet[:, None], n_grids, axis=1)
    elif pet.ndim == 2 and pet.shape == precip.shape:
        pet = pet.copy()
    else:
        raise ValueError(
            "pet_mm_h must be a scalar, [time] array or [time, grid] array"
        )
    if not np.isfinite(pet).all() or np.any(pet < 0.0):
        raise ValueError("pet_mm_h must contain finite non-negative values")
    state = _copy_and_validate_state(initial_state, params, n_grids)
    # [XAJ-CORE-02] Identical numeric kernel for calibration and replay.
    values = (params.KC, params.WUM, params.WLM, params.WDM, params.WM,
              params.C, params.B, params.SM, params.EX, params.KI, params.KG, params.IM)
    result = _array_kernel(precip, pet, tuple(state[name] for name in LAND_STATE_FIELDS), values, return_diagnostics)
    # A Numba typed.Dict is not pickle/deepcopy/JSON compatible with the
    # platform's checkpoint contract. Keep ordinary Python mappings at the API.
    output = (result[0], result[1], result[2], dict(result[3]))
    return output + (dict(result[4]),) if return_diagnostics else output


def _array_kernel(precip, pet, state, values, return_diagnostics):
    kc, wum, wlm, wdm, wm, c, b, sm, ex, ki, kg, im = values
    n_grids = precip.shape[1]
    swe, snow_liquid, wu, wl, wd, free_s, fr = state
    rs_values = np.empty(precip.shape, dtype=np.float32)
    ri_values = np.empty(precip.shape, dtype=np.float32)
    rg_values = np.empty(precip.shape, dtype=np.float32)
    shape = precip.shape if return_diagnostics else (0, n_grids)
    trace = {name: np.empty(shape, dtype=np.float64) for name in (
        "et_actual", "water_balance_error", "EU", "EL", "ED", "WU", "WL", "WD", "S", "FR", "infiltration", "net_rainfall", "pervious_runoff")}

    for time_index, rain in enumerate(precip):
        # [XAJ-WARMUP-CORE-01A] Three-layer evapotranspiration.
        old_state_s = free_s.copy()
        old_free = old_state_s * fr
        old_storage = swe + snow_liquid + wu + wl + wd + old_free
        demand = pet[time_index] * kc
        available_upper = rain + wu
        eu = np.minimum(available_upper, demand)
        rain_left = rain - np.minimum(rain, eu)
        wu = wu - (eu - np.minimum(rain, eu))
        remaining = demand - eu
        el = np.zeros(n_grids, dtype=float)
        ed = np.zeros(n_grids, dtype=float)
        mask_l1 = remaining > 1.0e-10
        mask_l2 = mask_l1 & (wl >= c * wlm)
        el[mask_l2] = np.minimum(
            wl[mask_l2],
            remaining[mask_l2] * wl[mask_l2] / max(wlm, 1.0e-10),
        )
        mask_l3 = mask_l1 & ~mask_l2 & (wl >= c * remaining)
        el[mask_l3] = np.minimum(wl[mask_l3], c * remaining[mask_l3])
        mask_deep = mask_l1 & ~mask_l2 & ~mask_l3
        el[mask_deep] = wl[mask_deep]
        ed[mask_deep] = np.minimum(
            wd[mask_deep],
            np.maximum(c * remaining[mask_deep] - el[mask_deep], 0.0),
        )
        wl -= el
        wd -= ed
        net = np.maximum(rain_left, 0.0)

        # [XAJ-WARMUP-CORE-01B] Tension-water capacity curve.
        pervious_input = net * (1.0 - im)
        w_before = wu + wl + wd
        w = np.clip(w_before, 0.0, wm)
        pe = np.maximum(pervious_input, 0.0)
        wmm = wm * (1.0 + b)
        a = wmm * (
            1.0
            - np.power(
                np.maximum(1.0 - w / wm, 0.0),
                1.0 / (1.0 + b),
            )
        )
        runoff = np.where(
            pe + a < wmm,
            pe
            - (wm - w)
            + wm
            * np.power(
                np.maximum(1.0 - (pe + a) / wmm, 0.0),
                1.0 + b,
            ),
            pe - (wm - w),
        )
        runoff = np.clip(runoff, 0.0, pe)
        w_after = np.minimum(wm, w + pe - runoff)
        addition = np.maximum(w_after - w_before, 0.0)
        take = np.minimum(np.maximum(wum - wu, 0.0), addition)
        wu += take
        addition -= take
        take = np.minimum(np.maximum(wlm - wl, 0.0), addition)
        wl += take
        addition -= take
        wd += np.minimum(np.maximum(wdm - wd, 0.0), addition)

        # [XAJ-WARMUP-CORE-01C] Free-water storage and RS/RI/RG separation.
        old_volume = np.maximum(free_s, 0.0) * np.clip(fr, 0.0, 1.0)
        no_runoff = runoff <= 1.0e-10
        fr_new = np.clip(runoff / np.maximum(pervious_input, 1.0e-10), 1.0e-10, 1.0)
        overflow = np.maximum(old_volume - sm * fr_new, 0.0)
        s = np.minimum(old_volume / fr_new, sm)
        smm = sm * (1.0 + ex)
        relative = np.clip(s / sm, 0.0, 1.0)
        au = smm * (
            1.0
            - np.power(
                np.maximum(1.0 - relative, 0.0),
                1.0 / (1.0 + ex),
            )
        )
        rs_curve = np.where(
            pervious_input + au < smm,
            fr_new
            * (
                pervious_input
                - sm
                + s
                + sm
                * np.power(
                    np.maximum(1.0 - (pervious_input + au) / smm, 0.0),
                    1.0 + ex,
                )
            ),
            fr_new * (pervious_input + s - sm),
        )
        rs_curve = np.clip(rs_curve, 0.0, runoff)
        rs = overflow + rs_curve
        s = np.minimum(
            sm,
            np.maximum(0.0, s + (runoff - rs_curve) / fr_new),
        )
        ri = s * ki * fr_new
        rg = s * kg * fr_new
        free_s = np.maximum(s * (1.0 - ki - kg), 0.0)
        rs = np.where(no_runoff, 0.0, rs)
        ri = np.where(no_runoff, ki * np.maximum(old_free, 0.0), ri)
        rg = np.where(no_runoff, kg * np.maximum(old_free, 0.0), rg)
        free_s = np.where(
            no_runoff,
            np.maximum(old_state_s * (1.0 - ki - kg), 0.0),
            free_s,
        )
        fr = np.where(no_runoff, np.clip(fr, 0.0, 1.0), fr_new)
        rs += net * im
        et_actual = eu + el + ed
        new_storage = swe + snow_liquid + wu + wl + wd + free_s * fr
        balance_error = rain - rs - ri - rg - et_actual - (new_storage - old_storage)
        rs_values[time_index] = rs
        ri_values[time_index] = ri
        rg_values[time_index] = rg
        if return_diagnostics:
            trace["et_actual"][time_index] = et_actual
            trace["water_balance_error"][time_index] = balance_error
            trace["EU"][time_index] = eu
            trace["EL"][time_index] = el
            trace["ED"][time_index] = ed
            trace["WU"][time_index] = wu
            trace["WL"][time_index] = wl
            trace["WD"][time_index] = wd
            trace["S"][time_index] = free_s
            trace["FR"][time_index] = fr
            trace["infiltration"][time_index] = pervious_input - runoff
            trace["net_rainfall"][time_index] = net
            trace["pervious_runoff"][time_index] = runoff
    final_state = {
        "swe": swe,
        "snow_liquid": snow_liquid,
        "WU": wu,
        "WL": wl,
        "WD": wd,
        "S": free_s,
        "FR": fr,
    }
    return rs_values, ri_values, rg_values, final_state, trace


# [XAJ-CORE-03] Compile the unchanged equations only when Numba is available.
# Unified cache/fallback policy keeps generated files outside model sources.
_python_array_kernel = _array_kernel
_array_kernel = optional_njit(_array_kernel)
