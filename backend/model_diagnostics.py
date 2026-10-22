"""Traceable water budgets and conditional lake-window residuals."""
import numpy as np
import pandas as pd

PARTITION_NAMES = {'duja': '杜加控制区', 'zajia_rest': '扎加藏布下游补充区',
                   'zagen': '扎根藏布', 'boqu': '波曲藏布', 'ali': '阿里藏布', 'fringe': '湖周补充区'}


def partition_budgets(times, source, select, diagnostic, states, initial, flows, routing,
                      spatial, parameters, initial_routing=None):
    """Mutually exclusive control partitions, in water volume and area depth.

    Duja and the small downstream remainder are disjoint; the full Zajia river
    is deliberately absent from the sum to avoid counting Duja twice.
    """
    result = []
    period = (times - pd.Timedelta(nanoseconds=1)).to_period('M')
    for key, label in PARTITION_NAMES.items():
        ids = [i for i, u in enumerate(spatial['units']) if u.get('partition') == key]
        if not ids:
            continue
        units = [spatial['units'][i] for i in ids]
        scale = np.array([u['areaKm2'] for u in units]) * 1000.
        ice = np.array([u['kind'] == 'glacier' for u in units])
        h, old = states[:, ids], initial[ids]
        stored = (h[:, :, 0] + h[:, :, 1] + np.where(ice, h[:, :, 7],
                  h[:, :, 2] + h[:, :, 3] + h[:, :, 4] + h[:, :, 5]*h[:, :, 6])) @ scale
        before = float((old[:, 0]+old[:, 1]+np.where(ice, old[:, 7],
                       old[:, 2]+old[:, 3]+old[:, 4]+old[:, 5]*old[:, 6])) @ scale)
        routed = flows['zajia']-flows['duja'] if key == 'zajia_rest' else flows[key]
        d = diagnostic[:, ids]
        table = pd.DataFrame({
            'precipitationM3': source['precipitation'][select][:, ids] @ scale,
            'evaporationBaselineM3': source['evaporation'][select][:, ids] @ scale,
            'actualEtM3': d[:, :, 6] @ scale,
            'iceExternalM3': (d[:, :, 5]+d[:, :, 8]) @ scale,
            'snowmeltM3': d[:, :, 4] @ scale,
            'icemeltM3': d[:, :, 5] @ scale,
            'generatedRunoffM3': d[:, :, :4].sum(axis=2) @ scale,
            'routedRunoffM3': routed*3600.,
            'unitStorageChangeM3': np.diff(np.r_[before, stored]),
            'routingStorageChangeM3': np.diff(np.r_[(initial_routing or {}).get(key, 0.), routing[key]]),
        }, index=times)
        table['balanceErrorM3'] = (table.precipitationM3+table.iceExternalM3-table.actualEtM3-
                                  table.routedRunoffM3-table.unitStorageChangeM3-table.routingStorageChangeM3)
        area = float(scale.sum()/1000.)
        months = []
        for month, block in table.groupby(period):
            if month.start_time < times[0].normalize() and len(block) == 1:
                continue
            row = {'month': str(month), 'hours': len(block), 'complete': len(block) == month.days_in_month*24,
                   **{k: float(v) for k, v in block.sum().items()}}
            for k in ('precipitation', 'evaporationBaseline', 'actualEt', 'generatedRunoff', 'routedRunoff', 'iceExternal',
                      'unitStorageChange', 'routingStorageChange', 'snowmelt', 'icemelt'):
                row[k+'Mm'] = row[k+'M3'] / (area*1000.)
            months.append(row)
        result.append({'id': key, 'name': label, 'areaKm2': area, 'glacierAreaKm2': float(scale[ice].sum()/1000.),
                       'observationConstraint': '杜加日流量约束' if key == 'duja' else '参数移用，未独立验证',
                       'monthly': months, 'balanceMaxErrorM3': float(table.balanceErrorM3.abs().max())})
    return result


def flow_timing_report(times, flows, observation):
    """Compare only coincident complete days; missing days are never zero."""
    daily = pd.Series(flows['duja'], index=times).resample('D', closed='right', label='left').agg(['mean', 'count'])
    values = observation.get('flow', {})
    observed = pd.Series(list(values.values()), index=pd.to_datetime(list(values)), dtype=float)
    paired = pd.concat([daily['mean'].where(daily['count'] == 24).rename('sim'), observed.rename('obs')], axis=1).dropna()
    monthly = []
    for month, block in paired.groupby(paired.index.to_period('M')):
        o, s = block.obs, block.sim
        monthly.append({'month': str(month), 'matchedDays': len(block), 'calendarDays': month.days_in_month,
                        'observedMean': float(o.mean()), 'simulatedMean': float(s.mean()),
                        'observedVolumeM3': float(o.sum()*86400), 'simulatedVolumeM3': float(s.sum()*86400),
                        'biasM3s': float((s-o).mean()), 'pbias': float((s.sum()-o.sum())/o.sum()*100) if o.sum() else None})
    return {'monthly': monthly, 'definition': '仅匹配完整日，月表总量是观测匹配日累计，不外推缺测日。'}

def diagnose(times, source, select, diagnostic, states, initial, flows, routing, flux,
             levels, volumes, windows, parameters, spatial, mode, observation, initial_routing_volume=0., initial_partition_routing=None):
    areas = np.array([u['areaKm2'] for u in spatial['units']])
    scale = areas * 1000
    ice = np.array([u['kind'] == 'glacier' for u in spatial['units']])
    unit_volume = (states[:, :, 0] + states[:, :, 1] +
                   np.where(ice, states[:, :, 7], states[:, :, 2] + states[:, :, 3] +
                            states[:, :, 4] + states[:, :, 5] * states[:, :, 6])) @ scale
    initial_volume = float((initial[:, 0] + initial[:, 1] + np.where(
        ice, initial[:, 7], initial[:, 2] + initial[:, 3] + initial[:, 4] + initial[:, 5] * initial[:, 6])) @ scale)
    snow_volume = (states[:, :, 0] + states[:, :, 1]) @ scale
    old_snow = float((initial[:, 0] + initial[:, 1]) @ scale)
    frame = pd.DataFrame({
        'landPrecipitationM3': source['precipitation'][select] @ scale,
        'landEvapotranspirationM3': diagnostic[:, :, 6] @ scale,
        'generatedRunoffM3': diagnostic[:, :, :4].sum(axis=2) @ scale,
        'iceExternalM3': (diagnostic[:, :, 5] + diagnostic[:, :, 8]) @ scale,
        'snowmeltM3': diagnostic[:, :, 4] @ scale,
        'icemeltM3': diagnostic[:, :, 5] @ scale,
        'iceSublimationM3': diagnostic[:, :, 8] @ scale,
        'unitStorageChangeM3': np.diff(np.r_[initial_volume, unit_volume]),
        'snowStorageChangeM3': np.diff(np.r_[old_snow, snow_volume]),
        'lakeInflowM3': flows['total'] * 3600,
        'lakePrecipitationM3': source['lakeRain'][select] / 1000 * spatial['lakeAreaKm2'] * 1e6,
        'lakeEvaporationM3': source['lakeEvaporation'][select] * parameters['lakeCoefficient'] / 1000 * spatial['lakeAreaKm2'] * 1e6,
    }, index=times)
    route_total = sum(routing.values())
    frame['routingStorageChangeM3'] = np.diff(np.r_[initial_routing_volume, route_total])
    frame['catchmentBalanceErrorM3'] = frame.landPrecipitationM3 + frame.iceExternalM3 - frame.landEvapotranspirationM3 - frame.lakeInflowM3 - frame.unitStorageChangeM3 - frame.routingStorageChangeM3
    frame['landBalanceErrorM3'] = frame.landPrecipitationM3 + frame.iceExternalM3 - frame.landEvapotranspirationM3 - frame.generatedRunoffM3 - frame.unitStorageChangeM3
    # End-hour grouping: midnight belongs to the previous interval and month.
    period = (times - pd.Timedelta(nanoseconds=1)).to_period('M')
    monthly = []
    for month, group in frame.groupby(period):
        if month.start_time < times[0].normalize() and len(group) == 1:
            continue
        row = {'month': str(month), 'hours': len(group), 'complete': len(group) == month.days_in_month * 24,
               **{key: float(value) for key, value in group.sum().items()}}
        row['lakeNetBalanceM3'] = row['lakeInflowM3'] + row['lakePrecipitationM3'] - row['lakeEvaporationM3']
        monthly.append(row)
    observations = pd.Series(list(observation['level'].values()), index=pd.to_datetime(list(observation['level'])), dtype=float)
    daily = frame.resample('D', closed='right', label='left').sum()
    counts = frame.lakeInflowM3.resample('D', closed='right', label='left').count()
    daily.loc[counts < 24] = np.nan
    windows_report = []
    for window in windows:
        ids = np.flatnonzero((times >= window['start']) & (times <= window['end']) & np.isfinite(levels))
        if not len(ids):
            continue
        first, last = times[ids[0]], times[ids[-1]]
        start_day, end_day = first.normalize(), last.normalize()
        # Compare daily-mean level changes with midpoint-to-midpoint flux.
        # Trapezoidal half weights at endpoints avoid treating daily means as day-end levels.
        obs = observations.loc[(observations.index >= start_day) & (observations.index <= end_day)].dropna()
        residual = None
        if len(obs) >= 2:
            a, b = obs.index[0], obs.index[-1]
            block = daily.loc[a:b]
            if block.notna().all().all() and len(block) == (b-a).days+1:
                weights = np.ones(len(block)); weights[[0, -1]] = .5
                q = float(weights @ block.lakeInflowM3)
                rain = float(weights @ block.lakePrecipitationM3)
                evap = float(weights @ block.lakeEvaporationM3)
                observed = float((obs.iloc[-1] - obs.iloc[0]) * spatial['lakeAreaKm2'] * 1e6)
                residual = {'start': a.date().isoformat(), 'end': b.date().isoformat(), 'approximation': 'daily-mean-midpoint-trapezoid',
                            'observedStorageChangeM3': observed, 'inflowM3': q, 'precipitationM3': rain,
                            'evaporationM3': evap, 'unexplainedNetInputM3': observed-q-rain+evap,
                            'unexplainedLevelChangeM': (observed-q-rain+evap)/(spatial['lakeAreaKm2']*1e6)}
        windows_report.append({'id': window['id'], 'start': first.isoformat(), 'end': last.isoformat(),
                               'initialization': window['method'], 'conditionalOnObservedStart': True, 'openWaterIndependentlyVerified': False,
                               'windowVolumeChangeM3': float(volumes[ids[-1]]), 'residual': residual})
    partitions = partition_budgets(times, source, select, diagnostic, states, initial, flows, routing, spatial,
                                   parameters, initial_partition_routing)
    return {'schemaVersion': 2, 'routingMode': mode, 'monthly': monthly, 'lakeWindows': windows_report,
            'partitions': partitions, 'flowTiming': flow_timing_report(times, flows, observation) if 'duja' in flows else {},
            'partitionDefinition': '六个互斥产汇流分区；杜加与扎加下游补充区合为扎加藏布，求和时不重复加入整条扎加。',
            'catchmentBalanceMaxErrorM3': float(np.max(np.abs(frame.catchmentBalanceErrorM3))), 'landBalanceMaxErrorM3': float(np.max(np.abs(frame.landBalanceErrorM3))),
            'definitions': ['融雪和融冰是产汇流内部补给，不能再次加到入湖流量。',
                            '冰川融冰与裸冰升华均消耗固定边界之外未显式维护的冰体质量；外部冰体供水单独列账。',
                            '湖泊逐时净收支全年连续累计为相对水当量蓄水；冰期不据此宣称可靠水面高程。',
                            '实测窗口湖位是已知起始湖位条件下的历史模拟，不等同于不校正起点的多年自由预报。',
                            '可用观测的连续窗口不等于独立判定的无冰期，须结合现场或遥感冰情核验。',
                            '窗口未解释净入水是收支残差，不能直接认定为地下水或外流。',
                            '窗口残差以日均水位近似日中水位计算，端点半权重；不是严格逐时闭合检验。']}
