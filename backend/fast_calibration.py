"""Duja-only evaluation using the same hourly physics and routing as simulation."""
from collections import OrderedDict
from copy import deepcopy
import math

import numpy as np
import pandas as pd

from .calibration_config import ROUTING_KEYS, flow_objective
from .common import digest
from .grid_simulation import unit_forcing
from .model_core import PARAMETER_NAMES, initial_states, normalize_initial_hydrology, njit, parameters, unit_step
from .routing import route_partition

try:
    import numba
    _search_kernel=numba.njit(cache=True,nogil=True)
except ImportError:
    _search_kernel=njit


@_search_kernel
def _sources(rain, temperature, evaporation, offsets, glaciers, areas, state, vector):
    """Stream the existing unit_step; do not allocate unused state histories."""
    hours, count = rain.shape
    volumes = np.zeros((hours, 4), dtype=np.float64)
    maximum = 0.
    for t in range(hours):
        for u in range(count):
            values = unit_step(rain[t, u], temperature[t, u] + offsets[u] * vector[20],
                               evaporation[t, u], state[u], glaciers[u], vector)
            for k in range(4):
                volumes[t, k] += values[k] * areas[u] * 1000.
            maximum = max(maximum, abs(values[7]))
    return volumes, maximum


def array_metrics(observed, simulated):
    o, s = np.asarray(observed, float), np.asarray(simulated, float)
    if not len(o) or not np.isfinite(o).all() or not np.isfinite(s).all():
        raise ValueError('本次率定缺少固定日期的有效流量')
    error = s-o
    variance = np.sum((o-o.mean())**2)
    log_o, log_s = np.log1p(np.maximum(o, 0)), np.log1p(np.maximum(s, 0))
    log_variance = np.sum((log_o-log_o.mean())**2)
    kge = None
    if len(o)>1 and np.std(o)>1e-10 and np.std(s)>1e-10 and abs(o.mean())>1e-10:
        r = np.corrcoef(o, s)[0, 1]
        kge = 1-math.sqrt((r-1)**2+(np.std(s)/np.std(o)-1)**2+(s.mean()/o.mean()-1)**2)
    observed_volume, simulated_volume = float(o.sum()*86400), float(s.sum()*86400)
    return {'count':len(o), 'nse':float(1-np.sum(error**2)/variance) if variance>1e-12 else None,
            'logNse':float(1-np.sum((log_s-log_o)**2)/log_variance) if log_variance>1e-12 else None,
            'kge':float(kge) if kge is not None else None, 'rmse':float(np.sqrt(np.mean(error**2))),
            'mae':float(np.mean(abs(error))), 'bias':float(error.mean()),
            'pbias':float(100*error.sum()/o.sum()) if abs(o.sum())>1e-10 else None,
            'observedMean':float(o.mean()), 'simulatedMean':float(s.mean()),
            'observedVolumeM3':observed_volume, 'simulatedVolumeM3':simulated_volume,
            'pairedVolumeErrorM3':simulated_volume-observed_volume}


class DujaEvaluator:
    def __init__(self, source, request, end=None):
        spatial = deepcopy(source['spatial'])
        selected = [i for i,u in enumerate(spatial['units']) if u['partition']=='duja']
        if not selected:
            raise ValueError('空间资料缺少杜加控制区')
        spatial['units'] = [spatial['units'][i] for i in selected]
        if spatial.get('aggregation'):
            spatial['aggregation'] = [spatial['aggregation'][i] for i in selected]
        self.units = spatial['units']
        times = source['times']
        mask = (times>=pd.Timestamp(request['start'])) & (times<=pd.Timestamp(end or request['end']))
        ids = np.flatnonzero(mask)
        self.times = times[ids]
        if len(ids)<25 or not np.all(np.diff(self.times.asi8)==3600*10**9):
            raise ValueError('杜加率定需要连续小时驱动')
        base = {**request['parameters'], 'lapseRate':0.}
        from .land_precipitation import validate_manifest,validate_parameters
        self.land_correction = validate_manifest(source.get('manifest', {}))
        validate_parameters(self.land_correction,request['parameters'])
        self.rain, self.temperature, self.evaporation = unit_forcing(spatial, source['arrays'], ids, base, {},self.land_correction,times)
        if spatial.get('aggregation'):
            self.offsets = np.array([sum(m['areaWeight']*m['elevationOffsetM'] for m in g['members'])
                                     for g in spatial['aggregation']])
        else:
            self.offsets = np.array([u['elevation']-spatial['grids'][u['gridIndex']]['referenceElevation']
                                     for u in self.units])
        self.glaciers = np.array([u['kind']=='glacier' for u in self.units])
        self.areas = np.array([u['areaKm2'] for u in self.units])
        labels = (self.times-pd.Timedelta(nanoseconds=1)).normalize()
        self.days, self.bins = np.unique(labels.to_numpy(), return_inverse=True)
        self.days = pd.DatetimeIndex(self.days)
        self.counts = np.bincount(self.bins)
        values = request['_observations']['flow']
        self.observed = np.array([values.get(t.isoformat(), np.nan) for t in self.days])
        self.mode = request.get('routingMode','source-separated')
        self.initial_hydrology = normalize_initial_hydrology(request.get('initialHydrology'))
        self.profile = request['search'].get('objectiveProfile','balanced')
        self._sources_cache = OrderedDict()
        self.source_evaluations = 0
        self.routing_evaluations = 0

    def sources(self, candidate):
        from .land_precipitation import validate_parameters
        validate_parameters(self.land_correction,candidate)
        key = tuple(candidate[k] for k in PARAMETER_NAMES if k not in ROUTING_KEYS+['lakeCoefficient'])
        if key not in self._sources_cache:
            values = _sources(self.rain, self.temperature, self.evaporation, self.offsets,
                              self.glaciers, self.areas, initial_states(self.units, candidate, self.initial_hydrology),
                              np.array([candidate[k] for k in PARAMETER_NAMES]))
            if values[1]>1e-6:
                raise ValueError('杜加产流水量不闭合')
            self._sources_cache[key] = values
            self.source_evaluations += 1
            while len(self._sources_cache)>2:
                self._sources_cache.popitem(last=False)
        self._sources_cache.move_to_end(key)
        return self._sources_cache[key][0]

    def hourly(self, candidate):
        volumes = self.sources(candidate)
        self.routing_evaluations += 1
        return route_partition(volumes, {}, candidate, self.mode)[0]

    def daily(self, candidate):
        hourly = self.hourly(candidate)
        values = np.bincount(self.bins, weights=hourly, minlength=len(self.days))/24
        values[self.counts!=24] = np.nan
        return values

    def mask(self, start, end):
        mask = (self.days>=pd.Timestamp(start)) & (self.days<pd.Timestamp(end))
        return mask & (self.counts==24) & np.isfinite(self.observed)

    def assess(self, candidate, start, end):
        mask = self.mask(start, end)
        metric = array_metrics(self.observed[mask], self.daily(candidate)[mask])
        metric['countByYear'] = {str(y):int(np.sum(self.days[mask].year==y)) for y in set(self.days[mask].year)}
        return metric

    def objective(self, candidate, mask):
        metric = array_metrics(self.observed[mask], self.daily(candidate)[mask])
        return flow_objective(metric, self.profile)[0], metric

    def evidence(self, mask):
        pairs = [{'date':t.isoformat(),'observed':float(v)}
                 for t,v in zip(self.days[mask], self.observed[mask])]
        return {'fixedTrainingDates':[p['date'] for p in pairs],
                'fixedTrainingObservationSHA256':digest(pairs), 'trainingPairs':len(pairs),
                'calculationUnits':[u['id'] for u in self.units],
                'engine':'existing-unit-step-and-route-partition',
                'sourceCache':'fixed-land-and-snow-vector; routing-only proposals reuse runoff'}
