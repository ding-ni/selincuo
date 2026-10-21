"""Optional saturation-excess to infiltration-excess conversion process."""
from __future__ import annotations

import math

from ..core.types import NUMERICAL, RunoffConversionParams


# [CONVERT-01] Independent optional conversion module
def saturation_infiltration_conversion(
    net_rainfall: float,
    slope_degree: float,
    params: RunoffConversionParams,
):
    """Split direct infiltration-excess RS before standard XAJ generation.

    When disabled, all net rainfall is passed unchanged to the XAJ model.
    The conversion and XAJ branches therefore never double-count water.
    """
    net_rainfall = max(float(net_rainfall), 0.0)
    if not params.enabled or net_rainfall <= NUMERICAL.epsilon:
        return 0.0, net_rainfall, 0.0, float("inf")
    slope = max(float(slope_degree), 0.0)
    tangent = math.tan(math.radians(min(slope, NUMERICAL.maximum_slope_degree)))
    capacity = params.base_infiltration_capacity_mm_h / (
        1.0 + params.slope_reduction_coefficient * tangent
    )
    slope_activation = min(
        max((slope - params.slope_threshold_degree) / params.transition_width_degree, 0.0),
        1.0,
    )
    rain_activation = min(
        max(
            (net_rainfall - params.rain_intensity_threshold_mm_h)
            / params.transition_width_mm_h,
            0.0,
        ),
        1.0,
    )
    activation = slope_activation * rain_activation
    excess = max(net_rainfall - capacity, 0.0) * activation
    return excess, net_rainfall - excess, activation, capacity
