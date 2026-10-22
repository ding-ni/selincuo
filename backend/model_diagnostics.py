"""Traceable water budgets and conditional lake-window residuals."""
import numpy as np
import pandas as pd


def diagnose(times, source, select, diagnostic, states, initial, flows, routing, flux,
             levels, volumes, windows, parameters, spatial, mode, observation, initial_routing_volume=0.):
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
    return {'schemaVersion': 1, 'routingMode': mode, 'monthly': monthly, 'lakeWindows': windows_report,
            'catchmentBalanceMaxErrorM3': float(np.max(np.abs(frame.catchmentBalanceErrorM3))), 'landBalanceMaxErrorM3': float(np.max(np.abs(frame.landBalanceErrorM3))),
            'definitions': ['融雪和融冰是产汇流内部补给，不能再次加到入湖流量。',
                            '冰川融冰与裸冰升华均消耗固定边界之外未显式维护的冰体质量；外部冰体供水单独列账。',
                            '湖泊逐时净收支全年连续累计为相对水当量蓄水；冰期不据此宣称可靠水面高程。',
                            '实测窗口湖位是已知起始湖位条件下的历史模拟，不等同于不校正起点的多年自由预报。',
                            '可用观测的连续窗口不等于独立判定的无冰期，须结合现场或遥感冰情核验。',
                            '窗口未解释净入水是收支残差，不能直接认定为地下水或外流。',
                            '窗口残差以日均水位近似日中水位计算，端点半权重；不是严格逐时闭合检验。']}
