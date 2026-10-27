"""Explicit parameter regions; the lake has one basin-wide coefficient."""
from collections.abc import Mapping
import numpy as np
from .model_core import parameters, initial_states, PARAMETER_NAMES, run_kernel


def resolve_regions(units, base, overrides=None):
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, Mapping):
        raise ValueError('分区参数必须为对象')
    allowed = {u['partition'] for u in units}
    unknown = set(overrides)-allowed
    if unknown:
        raise ValueError('未知参数分区：'+', '.join(sorted(unknown)))
    normalized, resolved = {}, {}
    for key, patch in overrides.items():
        if not isinstance(patch, Mapping):
            raise ValueError('分区参数内容必须为对象')
        if 'lakeCoefficient' in patch:
            raise ValueError('湖面蒸发系数只能在全湖配置，不属于产汇流分区')
        p = parameters({**base, **patch})
        if patch:
            normalized[key] = {k: p[k] for k in sorted(patch)}
            resolved[key] = p
    return normalized, resolved


def region_initial_states(units, base, regions):
    if not regions:
        return initial_states(units, base)
    return np.concatenate([initial_states([u], regions.get(u['partition'], base)) for u in units])


def run_region_kernel(rain, temperature, evaporation, glaciers, state, units, base, regions):
    """Reuse the complete snow/ice kernel for each distinct parameter vector."""
    grouped = {}
    for i, unit in enumerate(units):
        p = regions.get(unit['partition'], base)
        vector = tuple(p[k] for k in PARAMETER_NAMES)
        grouped.setdefault(vector, []).append(i)
    d = np.empty((*rain.shape, 9), dtype=float)
    h = np.empty((*rain.shape, 8), dtype=float)
    for vector, indexes in grouped.items():
        ids = np.asarray(indexes)
        local_d, local_h, final = run_kernel(
            np.ascontiguousarray(rain[:, ids]), np.ascontiguousarray(temperature[:, ids]),
            np.ascontiguousarray(evaporation[:, ids]), glaciers[ids], state[ids].copy(),
            np.array(vector, dtype=float))
        d[:, ids], h[:, ids], state[ids] = local_d, local_h, final
    return d, h, state
