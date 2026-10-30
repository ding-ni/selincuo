"""Serializable objective used by both the rate-setting form and the solver."""
from copy import deepcopy
import math


DEFAULT_OBJECTIVE = {
    'version': 'continuous-flow-lake-v1',
    'weights': {'flow': .5, 'lake': .5},
    'flowWeights': {'nse': .5, 'logNse': .25, 'volume': .25},
    'lakeWeights': {'rmse': .25, 'trend': .25, 'amplitude': .25, 'peakTiming': .25},
    'lakeScales': {'rmseM': .1, 'trendErrorM': .1,
                   'amplitudeErrorM': .1, 'peakTimingErrorDays': 14.},
}


def validate_objective(value=None):
    value = deepcopy(DEFAULT_OBJECTIVE if value is None else value)
    if not isinstance(value, dict) or set(value) != set(DEFAULT_OBJECTIVE):
        raise ValueError('请提供完整的流量与湖位目标设置')
    if value['version'] != DEFAULT_OBJECTIVE['version']:
        raise ValueError('不支持的率定目标版本')
    for group in ('weights', 'flowWeights', 'lakeWeights'):
        values = value[group]
        if (not isinstance(values, dict) or set(values) != set(DEFAULT_OBJECTIVE[group]) or
                any(type(v) not in (int, float) or not math.isfinite(v) or v < 0
                    for v in values.values()) or abs(sum(values.values()) - 1.) > 1e-10):
            raise ValueError('目标权重须为非负数且合计为1：' + group)
    if min(value['weights'].values()) <= 0:
        raise ValueError('完整率定同时使用流量与湖位目标')
    scales = value['lakeScales']
    if (not isinstance(scales, dict) or set(scales) != set(DEFAULT_OBJECTIVE['lakeScales']) or
            any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0
                for v in scales.values())):
        raise ValueError('湖位评分尺度须为正数')
    return value


def combined_score(flow, lake, definition):
    weights = definition['weights']
    return float(weights['flow'] * flow + weights['lake'] * lake)
