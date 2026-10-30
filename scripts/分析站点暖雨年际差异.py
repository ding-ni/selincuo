"""Describe paired gauge/grid warm-rain differences without changing inputs."""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.common import now, read_json, sha_file
from backend.database import Database
from backend.grid_simulation import prepare
from 站点暖雨订正对照 import INPUT_ID, SPATIAL_ID, season_bounds, station_samples


def describe(frame):
    paired = frame[frame.eligible.fillna(False)].copy()
    if paired.empty:
        return {'coveredDays': int(frame.coverage.sum()), 'pairedDays': 0}
    gauge = paired.obsWarm.astype(float)
    era = paired.eraWarm.astype(float)
    gauge_wet, era_wet = gauge >= .1, era >= .1
    categories = {'bothWet': gauge_wet & era_wet,
                  'gaugeOnlyWet': gauge_wet & ~era_wet,
                  'eraOnlyWet': ~gauge_wet & era_wet,
                  'bothDry': ~gauge_wet & ~era_wet}
    contrasts = paired.assign(gaugeMinusEra=gauge-era, absoluteDifference=(gauge-era).abs())
    largest = contrasts.nlargest(8, 'absoluteDifference')
    return {'coveredDays': int(frame.coverage.sum()), 'pairedDays': int(len(paired)),
        'gaugeMm': float(gauge.sum()), 'eraMm': float(era.sum()),
        'gaugeToEra': float(gauge.sum()/era.sum()) if era.sum() > 0 else None,
        'rainDayThresholdMm': .1,
        'gaugeRainDays': int(gauge_wet.sum()), 'eraRainDays': int(era_wet.sum()),
        'dryWetCategories': {name: {'days': int(mask.sum()),
            'gaugeMm': float(gauge[mask].sum()), 'eraMm': float(era[mask].sum())}
            for name, mask in categories.items()},
        'largestDailyDifferences': [{'date': str(date.date()), 'gaugeMm': float(row.obsWarm),
            'eraMm': float(row.eraWarm), 'source': row.source}
            for date, row in largest.iterrows()]}


def main():
    db = Database()
    frozen = read_json(db.workspace / 'inputs/products/INPUT-67a56a9abcc50dc4a9033ab6/manifest.json')
    profile = frozen['landPrecipitationCorrection']
    source = prepare(db, INPUT_ID, SPATIAL_ID)
    samples, conflicts, digest = station_samples(db, source, profile['confirmedEventSources'])
    if digest != profile['observationSnapshotSHA256']:
        raise ValueError('站点资料快照已改变，不能把分析与已冻结输入混同')
    report = {'createdAt': now(), 'inputId': frozen['id'], 'observationSnapshotSHA256': digest,
        'method': '相同暖时配对日内，分别统计站雨与格点雨日、量和每日差值；雨日阈值0.1mm仅用于诊断，不改变倍率估计。',
        'interpretation': '统计差异描述可观测量；不得将全部差异归因于ERA偏差，设备降雪漏测、时间和代表性均可能影响。',
        'confirmedCoverage': profile['confirmedEventSources'], 'unresolvedConflicts': conflicts,
        'stations': {}}
    for station, item in samples.items():
        frame = pd.DataFrame(item['days']).set_index('date')
        seasonal, monthly = [], []
        for year in (2024, 2025, 2026):
            for season in ('DJF', 'MAM', 'JJA', 'SON'):
                start, end = season_bounds(year, season)
                part = frame[(frame.index >= start) & (frame.index < end)]
                seasonal.append({'year': year, 'season': season, 'expectedDays': int((end-start).days), **describe(part)})
            for month in range(1, 13):
                start = pd.Timestamp(year, month, 1)
                if start > pd.Timestamp('2026-10-20'):
                    break
                end = start + pd.DateOffset(months=1)
                part = frame[(frame.index >= start) & (frame.index < end)]
                monthly.append({'month': start.strftime('%Y-%m'), 'expectedDays': int((end-start).days), **describe(part)})
        report['stations'][station] = {'gridId': item['gridId'], 'seasonal': seasonal, 'monthly': monthly}
    folder = db.workspace / 'diagnostics' / ('站雨年际分析-' + uuid.uuid4().hex[:12])
    folder.mkdir(parents=True, exist_ok=False)
    target = folder / '站点与ERA暖雨过程对照.json'
    with target.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'path': str(target), 'sha256': sha_file(target),
        'jja': {s: [r for r in v['seasonal'] if r['season'] == 'JJA' and r['year'] in (2025, 2026)]
                for s, v in report['stations'].items()}}, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
