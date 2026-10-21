"""Snow accumulation, melt, refreezing, sublimation and drainage."""
from __future__ import annotations

from ..core.types import NUMERICAL, PHYSICAL, SnowParams
from .common import potential_melt


# [SNOW-01] Shared hourly snow process
def snow_step(swe, liquid, snowfall, rain_on_snow, temperature, pet_left, params: SnowParams, row):
    swe = max(float(swe), 0.0) + max(float(snowfall), 0.0)
    liquid = max(float(liquid), 0.0) + max(float(rain_on_snow), 0.0)

    # [SNOW-01A] Snowmelt and within-hour snow-exhaustion fraction
    solid_before_melt = swe
    melt_potential = potential_melt(
        temperature,
        params.snow_ddf,
        params.melt_temp,
        row,
        params.melt_method,
        params.default_albedo,
    )
    melt = min(solid_before_melt, melt_potential)
    if solid_before_melt <= NUMERICAL.epsilon:
        bare_fraction = 1.0
    elif melt_potential > NUMERICAL.epsilon and melt >= solid_before_melt - NUMERICAL.epsilon:
        bare_fraction = max(0.0, min(1.0, 1.0 - solid_before_melt / melt_potential))
    else:
        bare_fraction = 0.0
    swe -= melt
    liquid += melt

    # [SNOW-01B] Refreezing
    refreeze_potential = params.refreeze_factor * max(-float(temperature), 0.0) / PHYSICAL.hours_per_day
    refreeze = min(liquid, refreeze_potential)
    liquid -= refreeze
    swe += refreeze

    # [SNOW-01C] Snow sublimation consumes remaining PET
    sublimation = min(swe, max(float(pet_left), 0.0) * params.sublimation_fraction_pet)
    swe -= sublimation
    pet_left = max(float(pet_left) - sublimation, 0.0)

    # [SNOW-01D] Liquid-water holding capacity and drainage
    capacity = params.liquid_capacity_ratio * swe
    drainage = max(liquid - capacity, 0.0)
    liquid -= drainage
    if swe > NUMERICAL.epsilon:
        bare_fraction = 0.0
    return swe, liquid, melt, refreeze, sublimation, drainage, pet_left, bare_fraction
