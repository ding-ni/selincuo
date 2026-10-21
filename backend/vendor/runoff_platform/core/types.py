"""平台公共参数、状态和上下文类型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
import math


@dataclass(frozen=True)
class PhysicalConstants:
    hours_per_day: float = 24.0
    seconds_per_hour: float = 3600.0
    millimeters_per_meter: float = 1000.0
    water_density_kg_m3: float = 1000.0
    latent_heat_fusion_j_kg: float = 334000.0


@dataclass(frozen=True)
class NumericalSettings:
    epsilon: float = 1e-10
    supported_time_step_hours: float = 1.0
    balance_tolerance_mm: float = 1e-6
    maximum_slope_degree: float = 89.0


PHYSICAL = PhysicalConstants()
NUMERICAL = NumericalSettings()


def daily_recession_to_hourly(k_day: float) -> float:
    return 1.0 - math.exp(-k_day / PHYSICAL.hours_per_day)


def daily_source_separation_to_hourly(
    total_outflow_fraction_day: float,
    groundwater_fraction: float,
) -> tuple[float, float]:
    """Convert a daily free-water outflow and split ratio to hourly KI/KG."""
    total_day = float(total_outflow_fraction_day)
    groundwater_share = float(groundwater_fraction)
    if not 0.0 <= total_day < 1.0:
        raise ValueError("K_total_day must be in [0, 1)")
    if not 0.0 <= groundwater_share <= 1.0:
        raise ValueError("groundwater_fraction must be in [0, 1]")
    total_hour = 1.0 - (1.0 - total_day) ** (1.0 / PHYSICAL.hours_per_day)
    kg_hour = total_hour * groundwater_share
    ki_hour = total_hour - kg_hour
    return ki_hour, kg_hour


def hourly_source_separation_to_daily(
    ki_hour: float,
    kg_hour: float,
) -> tuple[float, float]:
    """Return daily total outflow and groundwater share for hourly KI/KG."""
    total_hour = float(ki_hour) + float(kg_hour)
    if not 0.0 <= total_hour < 1.0 or min(float(ki_hour), float(kg_hour)) < 0.0:
        raise ValueError("hourly KI/KG must be non-negative and sum to less than 1")
    total_day = 1.0 - (1.0 - total_hour) ** PHYSICAL.hours_per_day
    groundwater_share = float(kg_hour) / total_hour if total_hour > 0.0 else 0.0
    return total_day, groundwater_share


@dataclass
class PETParams:
    shortwave_coefficient: float = 0.00045
    positive_temperature_coefficient: float = 0.002
    maximum_hourly_pet_mm: float = 0.65


@dataclass
class PhaseParams:
    all_snow_temp: float = -1.0
    all_rain_temp: float = 2.0


@dataclass
class SnowParams:
    melt_method: Literal["degree_day", "energy_balance"] = "degree_day"
    snow_ddf: float = 4.0
    melt_temp: float = 0.0
    liquid_capacity_ratio: float = 0.05
    refreeze_factor: float = 1.0
    sublimation_fraction_pet: float = 0.35
    default_albedo: float = 0.75


@dataclass
class GlacierParams:
    ice_ddf: float = 7.0
    ice_sublimation_fraction_pet: float = 0.25
    ice_sublimation_max_mm_h: float = 0.08
    reservoir_k: float = field(default_factory=lambda: daily_recession_to_hourly(0.5))
    phase: PhaseParams = field(default_factory=PhaseParams)
    snow: SnowParams = field(default_factory=SnowParams)
    pet: PETParams = field(default_factory=PETParams)


@dataclass
class RunoffConversionParams:
    enabled: bool = False
    base_infiltration_capacity_mm_h: float = 20.0
    slope_threshold_degree: float = 20.0
    rain_intensity_threshold_mm_h: float = 20.0
    slope_reduction_coefficient: float = 1.0
    transition_width_degree: float = 10.0
    transition_width_mm_h: float = 10.0


@dataclass
class LandParams:
    """三水源新安江参数；WDM由WM-WUM-WLM计算。"""
    phase: PhaseParams = field(default_factory=PhaseParams)
    snow: SnowParams = field(default_factory=SnowParams)
    pet: PETParams = field(default_factory=PETParams)
    KC: float = 0.89
    WM: float = 150.0
    WUM: float = 20.0
    WLM: float = 80.0
    C: float = 0.37
    B: float = 0.16
    SM: float = 15.0
    EX: float = 1.6
    KI: float = 0.0202135350180443
    KG: float = 0.0249300265222546
    source_coefficient_time_step_hours: float = 1.0
    IM: float = 0.0055
    CI: float = 0.76
    CG: float = 0.988
    KE: float = 1.0
    XE: float = 0.34
    conversion: RunoffConversionParams = field(default_factory=RunoffConversionParams)

    @property
    def WDM(self) -> float:
        return self.WM - self.WUM - self.WLM


@dataclass
class GridInfo:
    unit_id: str = "GRID-001"
    zone_type: Literal["GLACIER", "NON_GLACIER"] = "NON_GLACIER"
    area_km2: float = 1.0
    elevation_m: float = 0.0
    slope_degree: float = 0.0
    soil_depth_m: float = 2.5


@dataclass
class RunContext:
    run_id: str = "RUN-LOCAL"
    scenario_id: str = "BASE"
    param_set_id: str = "DEFAULT"
    time_step_hours: float = 1.0
    simulation_mode: Literal["SINGLE", "CONTINUOUS", "EVENT"] = "SINGLE"


@dataclass
class GlacierState:
    swe: float = 120.0
    snow_liquid: float = 2.0
    glacier_storage: float = 5.0


@dataclass
class LandState:
    swe: float = 10.0
    snow_liquid: float = 0.0
    WU: float = 12.0
    WL: float = 55.0
    WD: float = 30.0
    S: float = 5.0
    FR: float = 0.5
