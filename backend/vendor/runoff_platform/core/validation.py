"""参数、状态和驱动数据的集中校验。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .types import GridInfo, LandParams, LandState, GlacierState, NUMERICAL


class ModelValidationError(ValueError):
    pass


def validate_forcing(meteo: pd.DataFrame, require_hourly: bool = True) -> None:
    errors = []
    for name in ("precipitation", "temperature"):
        if name not in meteo.columns:
            errors.append(f"缺少气象字段 {name}")
        elif not np.isfinite(pd.to_numeric(meteo[name], errors="coerce")).all():
            errors.append(f"气象字段 {name} 含缺测或非数值")
    if "precipitation" in meteo and (meteo["precipitation"] < 0).any():
        errors.append("降水不能为负")
    if not isinstance(meteo.index, pd.DatetimeIndex):
        errors.append("气象索引必须为DatetimeIndex")
    elif meteo.index.duplicated().any():
        errors.append("气象时间存在重复")
    elif require_hourly and len(meteo) > 1:
        delta = meteo.index.to_series().diff().dropna()
        if not (delta == pd.Timedelta(hours=1)).all():
            errors.append("气象时间必须严格连续逐小时")
    if errors:
        raise ModelValidationError("; ".join(errors))


def validate_grid(grid: GridInfo) -> None:
    if grid.area_km2 <= 0:
        raise ModelValidationError("grid.area_km2必须大于0")
    if not 0 <= grid.slope_degree < 90:
        raise ModelValidationError("grid.slope_degree必须位于[0,90)")


def validate_land_params(params: LandParams) -> None:
    errors = []
    if params.WM <= 0 or params.WUM < 0 or params.WLM < 0 or params.WDM < 0:
        errors.append("必须满足WM>=WUM+WLM且各层容量非负")
    if params.B < 0 or params.SM <= 0 or params.EX < 0:
        errors.append("B、SM、EX范围不合法")
    if params.KI < 0 or params.KG < 0 or params.KI + params.KG > 1:
        errors.append("必须满足KI>=0、KG>=0且KI+KG<=1")
    if params.source_coefficient_time_step_hours != NUMERICAL.supported_time_step_hours:
        errors.append("KI和KG必须是当前1小时时间步长的出流系数")
    if not 0 <= params.IM < 1:
        errors.append("IM必须是[0,1)的小数比例")
    c = params.conversion
    if c.base_infiltration_capacity_mm_h <= 0:
        errors.append("转换模块基础入渗能力必须大于0")
    if c.transition_width_degree <= 0 or c.transition_width_mm_h <= 0:
        errors.append("转换模块过渡宽度必须大于0")
    if errors:
        raise ModelValidationError("; ".join(errors))


def validate_land_state(state: LandState, params: LandParams) -> None:
    checks = [
        ("swe", state.swe, 0, float("inf")),
        ("snow_liquid", state.snow_liquid, 0, float("inf")),
        ("WU", state.WU, 0, params.WUM),
        ("WL", state.WL, 0, params.WLM),
        ("WD", state.WD, 0, params.WDM),
        ("S", state.S, 0, params.SM),
        ("FR", state.FR, 0, 1),
    ]
    errors = [f"{n}必须位于[{lo},{hi}]" for n, v, lo, hi in checks if not lo <= v <= hi]
    if errors:
        raise ModelValidationError("; ".join(errors))


def validate_glacier_state(state: GlacierState) -> None:
    if min(state.swe, state.snow_liquid, state.glacier_storage) < 0:
        raise ModelValidationError("冰川状态不能为负")
