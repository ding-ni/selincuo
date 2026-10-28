"""Hourly cold-region XAJ, ported from the preserved scalar kernel.

The batch kernel is accepted only against scalar reference and water balance
tests. No liquid-only shortcut is used for snow or glacier units.
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np

from .common import WORKSPACE

try:
    import numba
    numba.config.CACHE_DIR = str(WORKSPACE / 'numeric-cache')
    njit = numba.njit(cache=not getattr(sys, 'frozen', False))
except ImportError:
    def njit(function):
        return function

PARAMETER_NAMES = ['KC', 'WM', 'B', 'C', 'SM', 'EX', 'KI', 'KG', 'IM',
                   'snowDdf', 'iceDdf', 'snowTemperature', 'rainTemperature',
                   'liquidCapacity', 'refreezeFactor', 'snowSublimation',
                   'glacierRecessionDay', 'tauHours', 'lagHours', 'lakeCoefficient', 'lapseRate', 'interflowTauHours',
                   'baseflowTauHours', 'iceSublimationFraction', 'iceSublimationMaxMmHour']
DEFAULT_PARAMETERS = dict(zip(PARAMETER_NAMES, [
    .8, 150., .3, .15, 30., 1.2, .018, .012, .01,
    4., 7., -1., 2., .05, 1., .35, .5, 24., 0., .8, -.0065, 48., 720., .25, .08]))
PARAMETER_DEFINITIONS = [
    {'key': 'KC', 'label': '陆面蒸发系数', 'min': .2, 'max': 1.5, 'step': .01, 'unit': '', 'group': 'land'},
    {'key': 'WM', 'label': '张力水容量', 'min': 40, 'max': 300, 'step': 1, 'unit': 'mm', 'group': 'land'},
    {'key': 'B', 'label': '蓄水容量曲线指数', 'min': .02, 'max': .8, 'step': .01, 'unit': '', 'group': 'land'},
    {'key': 'C', 'label': '深层蒸发系数', 'min': .01, 'max': .5, 'step': .01, 'unit': '', 'group': 'land'},
    {'key': 'SM', 'label': '自由水容量', 'min': 5, 'max': 100, 'step': 1, 'unit': 'mm', 'group': 'land'},
    {'key': 'EX', 'label': '自由水容量曲线指数', 'min': .1, 'max': 2.5, 'step': .05, 'unit': '', 'group': 'land'},
    {'key': 'KI', 'label': '小时壤中流出流系数', 'min': .001, 'max': .15, 'step': .001, 'unit': 'h⁻¹', 'group': 'land'},
    {'key': 'KG', 'label': '小时地下出流系数', 'min': .0001, 'max': .1, 'step': .001, 'unit': 'h⁻¹', 'group': 'land'},
    {'key': 'IM', 'label': '不透水比例', 'min': 0, 'max': .2, 'step': .001, 'unit': '', 'group': 'land'},
    {'key': 'snowDdf', 'label': '融雪度日因子', 'min': 1, 'max': 10, 'step': .1, 'unit': 'mm/℃/d', 'group': 'snow'},
    {'key': 'iceDdf', 'label': '融冰度日因子', 'min': 2, 'max': 14, 'step': .1, 'unit': 'mm/℃/d', 'group': 'snow'},
    {'key': 'snowTemperature', 'label': '全雪温度', 'min': -5, 'max': 1, 'step': .1, 'unit': '℃', 'group': 'snow'},
    {'key': 'rainTemperature', 'label': '全雨温度', 'min': 0, 'max': 5, 'step': .1, 'unit': '℃', 'group': 'snow'},
    {'key': 'liquidCapacity', 'label': '雪层持水比例', 'min': .01, 'max': .15, 'step': .01, 'unit': '', 'group': 'snow'},
    {'key': 'refreezeFactor', 'label': '雪内再冻结因子', 'min': 0, 'max': 3, 'step': .1, 'unit': 'mm/℃/d', 'group': 'snow'},
    {'key': 'snowSublimation', 'label': '升华分配比例', 'min': 0, 'max': 1, 'step': .05, 'unit': '', 'group': 'snow'},
    {'key': 'iceSublimationFraction', 'label': '裸冰升华潜在蒸发比例', 'min': 0, 'max': 1, 'step': .05, 'unit': '', 'group': 'snow'},
    {'key': 'iceSublimationMaxMmHour', 'label': '裸冰升华小时上限', 'min': 0, 'max': .4, 'step': .01, 'unit': 'mm/h', 'group': 'snow'},
    {'key': 'glacierRecessionDay', 'label': '冰川日调蓄常数', 'min': .05, 'max': 3, 'step': .05, 'unit': 'd⁻¹', 'group': 'snow'},
    {'key': 'tauHours', 'label': '子流域调蓄时间', 'min': 1, 'max': 168, 'step': 1, 'unit': 'h', 'group': 'routing'},
    {'key': 'lagHours', 'label': '传播滞时', 'min': 0, 'max': 72, 'step': 1, 'unit': 'h', 'group': 'routing'},
    {'key': 'lakeCoefficient', 'label': '湖面蒸发转换系数', 'min': .2, 'max': 1.5, 'step': .01, 'unit': '', 'group': 'lake'},
    {'key': 'lapseRate', 'label': '气温高程递减率', 'min': -.01, 'max': -.002, 'step': .0001, 'unit': '℃/m', 'group': 'snow'},
]
PARAMETER_DEFINITIONS += [
    {'key': 'interflowTauHours', 'label': '壤中调蓄时间', 'min': 6, 'max': 240, 'step': 1, 'unit': 'h', 'group': 'routing'},
    {'key': 'baseflowTauHours', 'label': '地下退水时间', 'min': 168, 'max': 2160, 'step': 24, 'unit': 'h', 'group': 'routing'},
]
CALIBRATION_ROLE = {key: stage for stage, keys in (
    ('land-runoff', ['KC', 'WM', 'B', 'SM', 'KI', 'KG']),
    ('routing', ['tauHours', 'lagHours', 'interflowTauHours', 'baseflowTauHours']),
    ('lake', ['lakeCoefficient']),
) for key in keys}
for definition in PARAMETER_DEFINITIONS:
    definition['calibrationRole'] = CALIBRATION_ROLE.get(definition['key'], 'fixed-prior')
    if definition['calibrationRole'] == 'fixed-prior':
        definition['parameterBasis'] = '沿用原平台/通用概念初值；色林错缺少对应过程实测，尚未独立验证'
    elif definition['calibrationRole'] == 'land-runoff':
        definition['parameterBasis'] = '候选率定参数；只受杜加单站日流量约束，不能代表未测分区'
    elif definition['calibrationRole'] == 'routing':
        definition['parameterBasis'] = '候选路由参数；冻结产流过程后由杜加日流量约束，需检验边界与初始状态影响'
    else:
        definition['parameterBasis'] = '候选湖面转换参数；仅由条件湖位窗口约束，缺湖面蒸发实测'

# A parameter's presence in the physical model is distinct from whether a
# Duja discharge series can identify it. Metadata does not change the kernel.
from .calibration_config import LAND_KEYS, SNOW_KEYS, ROUTING_KEYS, RUNOFF_KEYS
for definition in PARAMETER_DEFINITIONS:
    key = definition['key']
    stage = 'runoff' if key in LAND_KEYS else 'snow' if key in SNOW_KEYS else 'routing' if key in ROUTING_KEYS else None
    if key in ('iceSublimationFraction', 'iceSublimationMaxMmHour'):
        reason = '当前模型的裸冰升华不改变径流，不能用杜加流量率定。'
    elif key == 'lakeCoefficient':
        reason = '湖面蒸发系数不改变杜加流量，应按湖位资料独立评价。'
    elif key in SNOW_KEYS:
        reason = '作用取决于积雪、冰川和气温条件；缺少独立冰雪观测时，流量只能提供间接约束。'
    else:
        reason = '作用及可辨识性取决于本期气象、下垫面与流量资料。'
    definition['flowCalibration'] = {'allowed': stage is not None, 'stage': stage,
                                     'defaultSelected': key in RUNOFF_KEYS or key in ROUTING_KEYS,
                                     'condition': reason}
STATE_NAMES = ['swe', 'snow_liquid', 'WU', 'WL', 'WD', 'S', 'FR', 'glacier_storage']
DIAGNOSTIC_NAMES = ['RS', 'RI', 'RG', 'glacier_runoff', 'snowmelt', 'icemelt', 'et_actual', 'water_balance_error', 'ice_sublimation']


def parameters(values=None):
    values = values or {}
    unknown = set(values) - set(PARAMETER_NAMES)
    if unknown:
        raise ValueError(f'未知参数：{sorted(unknown)}')
    result = {**DEFAULT_PARAMETERS, **values}
    for definition in PARAMETER_DEFINITIONS:
        value = result[definition['key']]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not definition['min'] <= value <= definition['max']:
            raise ValueError(f"{definition['label']}超出范围")
    if result['KI'] + result['KG'] >= 1:
        raise ValueError('小时KI与KG之和必须小于1')
    if result['snowTemperature'] >= result['rainTemperature']:
        raise ValueError('全雪温度必须低于全雨温度')
    if result['baseflowTauHours'] < result['interflowTauHours']:
        raise ValueError('地下退水时间应不短于壤中调蓄时间')
    return result


def initial_states(units, params):
    states = np.zeros((len(units), len(STATE_NAMES)), dtype=np.float64)
    for i, unit in enumerate(units):
        if unit['kind'] == 'land':
            states[i, 2:5] = np.array([params['WM'] * 2 / 15, params['WM'] * 8 / 15, params['WM'] / 3]) * .5
    return states


@njit
def snow_step(swe, liquid, snowfall, rain_on_snow, temperature, pet, p):
    swe += snowfall
    liquid += rain_on_snow
    potential = p[9] * max(temperature, 0.) / 24.
    original_swe = swe
    melt = min(swe, potential)
    bare = 1. if original_swe <= 1e-10 else (max(0., 1. - original_swe / potential) if potential > 1e-10 and melt >= original_swe - 1e-10 else 0.)
    swe -= melt
    liquid += melt
    freeze = min(liquid, p[14] * max(-temperature, 0.) / 24.)
    swe += freeze
    liquid -= freeze
    sublimation = min(swe, pet * p[15])
    swe -= sublimation
    pet = max(0., pet - sublimation)
    drainage = max(0., liquid - p[13] * swe)
    liquid -= drainage
    if swe > 1e-10:
        bare = 0.
    return swe, liquid, melt, sublimation, drainage, pet, bare


@njit
def unit_step(precip, temp, e0, state, glacier, p):
    wm, wum, wlm = p[1], p[1] * 2. / 15., p[1] * 8. / 15.
    wdm = wm - wum - wlm
    old = state[0] + state[1] + (state[7] if glacier else state[2] + state[3] + state[4] + state[5] * state[6])
    sf = 1. if temp <= p[11] else (0. if temp >= p[12] else (p[12] - temp) / (p[12] - p[11]))
    snowfall, rain = precip * sf, precip * (1. - sf)
    rain_snow = rain if state[0] > 1e-10 or snowfall > 1e-10 else 0.
    pet = e0 if glacier else e0 * p[0]
    swe, liquid, melt, sublimation, drainage, pet, bare = snow_step(state[0], state[1], snowfall, rain_snow, temp, pet, p)
    state[0], state[1] = swe, liquid
    rs, ri, rg, gr, imelt, et = 0., 0., 0., 0., 0., sublimation
    ice_sub = 0.
    if glacier:
        if swe <= 1e-10:
            imelt = p[10] * max(temp, 0.) / 24. * bare
            ice_sub = min(pet * p[23], p[24]) * bare
        state[7] += drainage + rain - rain_snow + imelt
        gr = state[7] * (1. - math.exp(-p[16] / 24.))
        state[7] -= gr
        et += ice_sub
        updated = swe + liquid + state[7]
        balance = precip + imelt + ice_sub - gr - et - (updated - old)
    else:
        rainfall = rain - rain_snow + drainage
        eu = min(rainfall + state[2], pet)
        used = min(rainfall, eu)
        rainfall -= used
        state[2] -= eu - used
        remaining = pet - eu
        el, ed = 0., 0.
        if remaining > 1e-10:
            if state[3] >= p[3] * wlm:
                el = min(state[3], remaining * state[3] / max(wlm, 1e-10))
            elif state[3] >= p[3] * remaining:
                el = min(state[3], p[3] * remaining)
            else:
                el = state[3]
                ed = min(state[4], max(p[3] * remaining - el, 0.))
        state[3] -= el
        state[4] -= ed
        et += eu + el + ed
        w = min(wm, max(0., state[2] + state[3] + state[4]))
        pe = rainfall * (1. - p[8])
        runoff = 0.
        if pe > 1e-10:
            wmm = wm * (1. + p[2])
            a = wmm * (1. - (1. - w / wm) ** (1. / (1. + p[2])))
            runoff = pe - (wm - w) + wm * (1. - (pe + a) / wmm) ** (1. + p[2]) if pe + a < wmm else pe - (wm - w)
            runoff = min(pe, max(0., runoff))
        addition = min(wm - w, pe - runoff)
        take = min(max(0., wum - state[2]), addition)
        state[2] += take
        addition -= take
        take = min(max(0., wlm - state[3]), addition)
        state[3] += take
        addition -= take
        state[4] += min(max(0., wdm - state[4]), addition)
        s, fr = state[5], state[6]
        if runoff <= 1e-10:
            ri, rg = s * fr * p[6], s * fr * p[7]
            s *= 1. - p[6] - p[7]
        else:
            old_volume = max(s, 0.) * fr
            fr = min(1., max(1e-10, runoff / max(pe, 1e-10)))
            overflow = max(old_volume - p[4] * fr, 0.)
            s = min(p[4], old_volume / fr)
            smm = p[4] * (1. + p[5])
            au = smm * (1. - (1. - min(1., max(0., s / p[4]))) ** (1. / (1. + p[5])))
            curve = fr * (pe - p[4] + s + p[4] * (1. - (pe + au) / smm) ** (1. + p[5])) if pe + au < smm else fr * (pe + s - p[4])
            curve = min(runoff, max(0., curve))
            rs = overflow + curve
            s = min(p[4], max(0., s + (runoff - curve) / fr))
            ri, rg = s * fr * p[6], s * fr * p[7]
            s *= 1. - p[6] - p[7]
        rs += rainfall * p[8]
        state[5], state[6] = s, fr
        updated = swe + liquid + state[2] + state[3] + state[4] + s * fr
        balance = precip - rs - ri - rg - et - (updated - old)
    return rs, ri, rg, gr, melt, imelt, et, balance, ice_sub


@njit
def run_kernel(precipitation, temperature, evaporation, glaciers, state, p):
    hours, count = precipitation.shape
    diagnostics = np.empty((hours, count, 9), dtype=np.float64)
    history = np.empty((hours, count, 8), dtype=np.float64)
    for t in range(hours):
        for u in range(count):
            values = unit_step(precipitation[t, u], temperature[t, u], evaporation[t, u], state[u], glaciers[u], p)
            for k in range(9):
                diagnostics[t, u, k] = values[k]
            for k in range(8):
                history[t, u, k] = state[u, k]
    return diagnostics, history, state


@njit
def route_series(inflow_volume, storage, queue, tau, lag):
    output = np.empty(len(inflow_volume), dtype=np.float64)
    stored = np.empty(len(inflow_volume), dtype=np.float64)
    integer = int(math.floor(lag))
    fraction = lag - integer
    decay = math.exp(-1. / tau)
    for i in range(len(inflow_volume)):
        queue[integer] += inflow_volume[i] * (1. - fraction)
        queue[integer + 1] += inflow_volume[i] * fraction
        arrival = queue[0]
        for q in range(len(queue) - 1):
            queue[q] = queue[q + 1]
        queue[-1] = 0.
        new_storage = storage * decay + arrival * tau * (1. - decay)
        output[i] = storage + arrival - new_storage
        storage = new_storage
        stored[i] = storage + queue.sum()
    return output, stored, storage, queue
