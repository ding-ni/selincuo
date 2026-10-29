"""Replay final parameters under alternate real warmups and initial stores."""
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd

from .common import digest, read_json, sha_file, write_json
from .grid_simulation import run as simulate
from .model_core import normalize_initial_hydrology
from .warmup_diagnostics import budget, comparison, snapshot


def _jsonable(value):
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    return value


def run(db, request, source, final, folder, progress, cancelled):
    """Save resumable alternate calculations using the selected model vector."""
    anchor = final.get('initialization')
    if not anchor:
        raise ValueError('预热核验需要已经确定的湖位起算时刻')
    start = pd.Timestamp(request['start'])
    anchor_time = pd.Timestamp(anchor['time'])
    required_end = anchor_time.normalize() + pd.Timedelta(days=2) + pd.DateOffset(years=1)
    end = min(pd.Timestamp(request['end']), required_end)
    if end <= anchor_time:
        raise ValueError('湖位起算后没有可供预热核验的连续时段')
    initial = normalize_initial_hydrology(request.get('initialHydrology'))
    fraction = initial['soilFraction']
    reference = '%s-%s' % (start.date().isoformat(), fraction)
    cases = [(reference, start, fraction)]
    missing = []
    recent_start = start + pd.DateOffset(years=1)
    comparison_starts = sorted({recent_start, pd.Timestamp('2019-01-01'), pd.Timestamp('2020-01-01')})
    for time in comparison_starts:
        day = time.date().isoformat()
        if time == start:
            continue
        if time < anchor_time and time in source['times']:
            cases.append(('%s-%s' % (day, fraction), time, fraction))
        else:
            missing.append({'start': day, 'reason': '输入资料未覆盖该预热起点'})
    for other in (.1, .9):
        if other != fraction:
            cases.append(('%s-%s' % (start.date().isoformat(), other), start, other))
    identity = digest({'inputId': request['inputId'], 'inputManifest': source.get('manifest', {}),
        'spatialVersion': request['spatialVersion'], 'routingMode': request.get('routingMode'),
        'parameterTransfer': request.get('parameterTransfer'),
        'modelImplementation': request['_modelIdentity'], 'start': request['start'],
        'end': request['end'], 'initialHydrology': initial, 'parameters': final['parameters'],
        'initialization': anchor, 'observation': request['_observations'],
        'cases': [(name, timestamp.isoformat(), soil) for name, timestamp, soil in cases],
        'diagnosticEnd': end.isoformat(), 'workflowSHA256': sha_file(__file__),
        'diagnosticsSHA256': sha_file(Path(__file__).with_name('warmup_diagnostics.py'))})
    folder.mkdir(parents=True, exist_ok=True)
    descriptor = folder / 'identity.json'
    existing = read_json(descriptor)
    if existing and existing.get('identity') != identity:
        raise ValueError('预热核验输入已改变，请建立新任务')
    if not existing:
        write_json(descriptor, {'identity': identity})
    completed = read_json(folder / 'summary.json')
    if completed:
        if completed.get('identity') != identity:
            raise ValueError('预热核验结果身份不一致')
        return completed
    states, comparisons = {}, {}
    for index, (name, case_start, case_fraction) in enumerate(cases):
        if cancelled():
            raise InterruptedError('已停止预热核验，已完成情景可接续')
        path = folder / (name + '.json')
        record = read_json(path)
        if record is None:
            options = {**deepcopy(request), 'start': case_start.isoformat(), 'end': end.isoformat(),
                'parameters': deepcopy(final['parameters']), 'partitionParameters': {},
                '_fixedInitialization': deepcopy(anchor), '_metricsOnly': True,
                'initialHydrology': {'soilFraction': case_fraction}}
            options.pop('_parentState', None)
            before = simulate(db, {**options, 'end': (anchor_time-pd.Timedelta(hours=1)).isoformat()},
                              prepared=source, cancelled=cancelled)
            at = simulate(db, {**options, 'end': anchor_time.isoformat()}, prepared=source, cancelled=cancelled)
            after = simulate(db, {**options, 'end': (anchor_time+pd.Timedelta(hours=1)).isoformat()},
                             prepared=source, cancelled=cancelled)
            alternate = final if name == reference else simulate(db, options, prepared=source, cancelled=cancelled)
            record = {'case': name, 'start': case_start.isoformat(), 'initialHydrology': options['initialHydrology'],
                'anchorStateBefore': snapshot(before), 'anchorStateAt': snapshot(at),
                'anchorStateAfter': snapshot(after),
                'comparison': None if name == reference else comparison(final, alternate)}
            write_json(path, _jsonable(record))
        states[name] = {'before': record['anchorStateBefore']['areaWeighted'],
                        'at': record['anchorStateAt']['areaWeighted'],
                        'after': record['anchorStateAfter']['areaWeighted']}
        if record['comparison'] is not None:
            comparisons[name] = record['comparison']
        progress(98.+(index+1)/len(cases), '最终参数预热核验 · %d/%d' % (index+1, len(cases)))
    recent_key='%s-%s' % (recent_start.date().isoformat(), fraction)
    recent_pass=bool(comparisons.get(recent_key,{}).get('passes',False))
    initial_keys=['%s-%s' % (start.date().isoformat(),other) for other in (.1,.9) if other != fraction]
    initial_pass=bool(initial_keys) and all(comparisons.get(key,{}).get('passes',False) for key in initial_keys)
    summary = _jsonable({'identity': identity, 'inputId': request['inputId'],
        'spatialVersion': request['spatialVersion'], 'referenceStart': start.isoformat(),
        'baselineCase': reference, 'parametersSHA256': digest(final['parameters']),
        'comparisons': comparisons, 'anchorStates': states, 'missingComparisons': missing,
        'recentComparisonStart':recent_start.isoformat(),'recentComparisonPass':recent_pass,
        'initialConditionPass':initial_pass,'allComparisonsPass':recent_pass and initial_pass,
        'budget': budget(final), 'basis': '起算后首个完整日起一年；正式判定采用相邻起点和同起点干湿初态，其余起点保留为历史对照。'})
    write_json(folder / 'summary.json', summary)
    return summary
