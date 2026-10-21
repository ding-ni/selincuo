"""Descriptive station/grid QC. No implicit forcing correction is applied."""
import numpy as np
import pandas as pd
from .common import write_json
from .observations import daily_series


def forcing_report(db, identifier, times, precipitation, spatial):
    rows = []
    grid_daily = pd.DataFrame(precipitation, index=times).resample('D', closed='right', label='left').sum()
    counts = pd.Series(1, index=times).resample('D', closed='right', label='left').sum()
    grid_daily.loc[counts < 24] = np.nan
    for station in db.rows("SELECT * FROM stations WHERE kind='rain' AND lon IS NOT NULL AND lat IS NOT NULL"):
        distances = [(g['lon'] - station['lon']) ** 2 * np.cos(np.radians(station['lat'])) ** 2 + (g['lat'] - station['lat']) ** 2 for g in spatial['grids']]
        index = int(np.argmin(distances))
        observed = daily_series(db, station['id'], 'precipitation')
        pair = pd.concat([observed.rename('observed'), grid_daily[index].rename('grid')], axis=1).dropna()
        monthly = []
        for month, group in pair.groupby(pair.index.to_period('M')):
            obs, grid = float(group.observed.sum()), float(group.grid.sum())
            monthly.append({'month': str(month), 'matchedDays': len(group), 'daysInMonth': month.days_in_month,
                            'observedMmOnMatchedDays': obs, 'gridMmOnMatchedDays': grid,
                            'biasMm': grid - obs, 'relativeBiasPercent': (grid - obs) / obs * 100 if obs else None})
        rows.append({'station': station['id'], 'grid': spatial['grids'][index]['id'], 'matchedDays': len(pair),
                     'daily': [{'date': t.isoformat(), 'observed': float(r.observed), 'grid': float(r.grid), 'biasMm': float(r.grid - r.observed)} for t, r in pair.iterrows()], 'monthly': monthly})
    report = {'inputId': identifier, 'correctionApplied': False, 'stations': rows,
              'limitations': ['按相同日期标签比较；原表降水统计日界需另行核对', '只比较双方可靠且完整的匹配日，未确认覆盖期的稀疏缺项不补零', '月偏差只覆盖匹配日，不冒充全月总量']}
    write_json(db.workspace / 'reports' / (identifier + '-station-qc.json'), report)
    return report
