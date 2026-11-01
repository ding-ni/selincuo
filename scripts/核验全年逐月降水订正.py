"""Read-only checks of the frozen monthly-total precipitation experiment.

Rebuilds the registered station factors from the saved monthly statistics.
This does not reconstruct unarchived daily samples, register an input, run a
model, change the adopted result, or alter the active calibration task.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import sqlite3
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.common import ROOT, WORKSPACE, digest, now, read_json, sha_file
from backend.land_precipitation import validate_manifest


INPUT_ID = 'INPUT-7e081ef81d2e24c22d80c5c4'
REPORT = 'diagnostics/全年逐月总降水订正-b6008807351a/全年逐月总降水订正报告.json'


def audit(workspace=WORKSPACE):
    workspace = Path(workspace).resolve()
    source_report = workspace / REPORT
    report = read_json(source_report)
    folder = workspace / 'inputs/products' / INPUT_ID
    manifest_path = folder / 'manifest.json'
    manifest = read_json(manifest_path)
    if not report or not manifest:
        raise ValueError('缺少冻结输入或原始逐月报告')
    validate_manifest(manifest)
    profile = manifest['landPrecipitationCorrection']
    checks = {
        'inputIdentity': report['correctedInputId'] == manifest['id'] == INPUT_ID,
        # The historical field is a canonical digest, not a file-byte SHA.
        'reportManifestCanonicalDigest': digest(manifest) == report['manifestSHA256'],
        'profileMatchesReport': profile == report['profile'],
        'landDigest': digest(profile) == manifest['landTransformationSHA256'],
        'forcingDigestMatchesReport': manifest['dataSHA256'] == report['forcingSHA256'],
        'forcingFileBytes': sha_file(folder / manifest['data']) == manifest['dataSHA256'],
    }
    database_path = workspace / 'database/hydrology.sqlite3'
    with sqlite3.connect('file:' + database_path.as_posix() + '?mode=ro', uri=True) as connection:
        stored = connection.execute('SELECT manifest FROM input_products WHERE id=?', (INPUT_ID,)).fetchone()
        parent = connection.execute('SELECT manifest FROM input_products WHERE id=?',
                                    (profile['parentInputId'],)).fetchone()
    checks['registeredManifestMatchesFile'] = bool(stored and json.loads(stored[0]) == manifest)
    parent = json.loads(parent[0]) if parent else None
    checks['parentRawForcingDigest'] = bool(parent and parent['dataSHA256'] == profile['parentDataSHA256'])
    pooled = defaultdict(lambda: defaultdict(lambda: [0., 0.]))
    failures = []
    eligible_count = 0
    for row in report['monthlyRecords']:
        coverage = row['validDays'] / row['days']
        eligible = (coverage >= profile['minimumCoverageFraction']
                    and row['wetDays'] >= profile['minimumWetDays']
                    and row['eraMm'] >= profile['minimumEraMonthlyMm'] and row['gaugeMm'] > 0)
        if (not math.isclose(coverage, row['coverage'], rel_tol=0., abs_tol=1e-12)
                or eligible != row['eligible']):
            failures.append({'station': row['station'], 'month': row['month']})
        if row['eligible']:
            eligible_count += 1
            bucket = pooled[row['station']][row['month'][-2:]]
            bucket[0] += row['gaugeMm']
            bucket[1] += row['eraMm']
    rebuilt = {station: {month: values[0] / values[1] for month, values in months.items()}
               for station, months in pooled.items()}
    registered = {station: item['monthFactors'] for station, item in profile['stations'].items()}
    checks['monthlyEligibility'] = not failures
    checks['pooledFactors'] = (
        rebuilt.keys() == registered.keys()
        and all(rebuilt[station].keys() == registered[station].keys()
                and all(math.isclose(value, registered[station][month], rel_tol=1e-14, abs_tol=1e-14)
                        for month, value in months.items()) for station, months in rebuilt.items()))
    checks['pooledFactorsMatchReport'] = registered == report['pooledFactors']
    represented = sorted({month for months in registered.values() for month in months})
    return {
        'createdAt': now(), 'inputId': INPUT_ID, 'checks': checks,
        'allChecksPass': all(checks.values()),
        'reportPath': str(source_report), 'reportFileSHA256': sha_file(source_report),
        'manifestFileSHA256': sha_file(manifest_path), 'manifestCanonicalSHA256': digest(manifest),
        'landTransformationSHA256': manifest['landTransformationSHA256'],
        'lakeTransformationSHA256': manifest['transformationSHA256'],
        'monthlyRecords': len(report['monthlyRecords']), 'eligibleStationYearMonths': eligible_count,
        'stationMonthFactorCount': sum(map(len, registered.values())), 'stationFactors': registered,
        'monthsWithAtLeastOneStationFactor': represented,
        'monthsWithoutStationFactor': [f'{m:02d}' for m in range(1, 13) if f'{m:02d}' not in represented],
        'eligibilityMismatches': failures,
        'notes': [
            '全年规则取消暖时门限；仅资料充分的月份存在实测估计倍率，其余月份保持ERA原值。',
            '本检查从冻结月统计重建倍率；原报告未归档逐日配对样本，不宣称已重新复现日表整编。',
            '设备类型未核实，按总降水水当量开展用户批准的试验。',
            '原manifest继承的旧湖面全年倍率说明已失效；实际执行以新land/lake规则为准，冻结原件不改。',
            'report.manifestSHA256为规范JSON内容摘要，与文件字节SHA256分别报告。',
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=WORKSPACE)
    parser.add_argument('--write', action='store_true', help='保存独立核验报告，不修改任何原件')
    args = parser.parse_args()
    report = audit(args.workspace)
    if args.write:
        target = ROOT / 'docs/evidence' / ('全年逐月降水输入核验-20261101-' + uuid.uuid4().hex[:10] + '.json')
        with target.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        print(target)
    print(json.dumps({key: report[key] for key in ('inputId', 'allChecksPass', 'checks', 'monthlyRecords',
        'eligibleStationYearMonths', 'stationMonthFactorCount', 'monthsWithoutStationFactor')}, ensure_ascii=False))
    if not report['allChecksPass']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
