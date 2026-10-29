"""Resumable Duja-constrained, lake-informed shared-parameter selection."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import copy, deepcopy
import json
import math
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from .calibration_config import flow_objective
from .common import digest, now, read_json, sha_file, within, write_json
from .fast_basin_calibration import BasinEvaluator
from .fast_calibration import DujaEvaluator, array_metrics
from .grid_simulation import run as simulate
from .lake_calibration import freeze_window, joint_assess, joint_fit
from .model_core import parameters


SOURCE_TASK = 'JOB-4934a5d35c9344a59353'
SCORE_VERSION = 'lake-four-equal-components-v1'
DEFAULT_RESCORE_WORKERS = 3
RESCORE_BLOCK_SIZE = 16


def candidate_source(db, identifier):
    """Resolve an existing immutable task and its all-data candidate trace."""
    task = db.task(identifier)
    if task.get('status') != 'succeeded' or task.get('kind') != 'two-stage-calibration':
        raise ValueError('参考候选必须来自已完成的两阶段率定')
    path = within(within(db.workspace / 'tasks', identifier), 'all-data/search-trace.jsonl')
    if not path.is_file():
        raise ValueError('参考率定缺少全部流量资料的候选轨迹')
    return task, path


def _rows(path):
    if not path.is_file():
        return []
    result = []
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                # A process can stop partway through the final append. The
                # incomplete line remains evidence and is ignored on resume.
                continue
    return result


def _append(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = ''
    if path.exists() and path.stat().st_size:
        with path.open('rb') as prior:
            prior.seek(-1, 2)
            if prior.read(1) != b'\n':
                prefix = '\n'
    with path.open('a', encoding='utf-8', newline='\n') as stream:
        stream.write(prefix + json.dumps(record, ensure_ascii=False, allow_nan=False,
                                         separators=(',', ':')) + '\n')
        stream.flush()


def _append_rescore_batch(path, records):
    """Append one already-ordered, bounded batch in a single file operation."""
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = ''
    if path.exists() and path.stat().st_size:
        with path.open('rb') as prior:
            prior.seek(-1, 2)
            if prior.read(1) != b'\n':
                prefix = '\n'
    encoded = [json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
               for record in records]
    with path.open('a', encoding='utf-8', newline='\n') as stream:
        stream.write(prefix + '\n'.join(encoded) + '\n')
        stream.flush()


def _candidate(initial, source_values, selected, bounds):
    if any(k not in source_values for k in selected):
        raise ValueError('参考轨迹缺少选中的参数')
    patch = {k: source_values[k] for k in selected}
    if any(not bounds[k][0] <= patch[k] <= bounds[k][1] for k in selected):
        return None
    try:
        return parameters({**initial, **patch})
    except (TypeError, ValueError):
        return None


def _flow(evaluator, candidate, mask):
    daily = evaluator.daily(candidate)
    metric = array_metrics(evaluator.observed[mask], daily[mask])
    metric['countByYear'] = {str(year): int(np.sum(evaluator.days[mask].year == year))
                             for year in sorted(set(evaluator.days[mask].year))}
    objective, _ = flow_objective(metric, evaluator.profile)
    return float(objective), metric


def _eligible(metric, constraints):
    return (metric.get('nse') is not None and metric.get('pbias') is not None and
            metric['nse'] >= constraints['minNse'] and
            abs(metric['pbias']) <= constraints['maxAbsPbias'])


def _rescore_worker_count(evaluator, workers):
    if type(workers) != int or not 1 <= workers <= 3:
        raise ValueError('杜加复评并行数须为1—3')
    # Test doubles and alternative evaluators retain the simple serial path.
    return workers if (isinstance(evaluator, DujaEvaluator) and
                       hasattr(evaluator, '_sources_cache') and hasattr(evaluator, 'sources')) else 1


def _rescore_worker(evaluator):
    # Immutable forcing arrays are shared. Mutable runoff caches and counters
    # are private to the worker, so concurrent candidates cannot corrupt them.
    worker = copy(evaluator)
    worker._sources_cache = OrderedDict()
    worker.source_evaluations = 0
    worker.routing_evaluations = 0
    return worker


def _score_block(evaluator, items, mask, selected, constraints):
    records = []
    for source_count, row, candidate, key in items:
        try:
            objective, metric = _flow(evaluator, candidate, mask)
            records.append((source_count, {'candidateId': key, 'sourceEvaluation': row.get('evaluation'),
                'parameters': {k: candidate[k] for k in selected}, 'flowObjective': objective,
                'flow': metric, 'eligible': _eligible(metric, constraints)}))
        except Exception as error:
            # Return completed work before the failed candidate; later blocks
            # are drained but not persisted, preserving source order on retry.
            return records, error
    return records, None


def rescore(evaluator, request, source_path, folder, progress, cancelled,
            workers=DEFAULT_RESCORE_WORKERS, block_size=RESCORE_BLOCK_SIZE):
    """Re-evaluate unique historical vectors in ordered, bounded blocks."""
    selected = request['search']['keys']
    bounds = request['search']['bounds']
    options = request['twoStage']
    constraints = options['flowConstraints']
    periods = request['periods']
    flow_start = min(periods['trainingStart'], periods['evaluationStart'])
    flow_end = max(periods['trainingEnd'], periods['evaluationEnd'])
    mask = evaluator.mask(flow_start, flow_end)
    if int(mask.sum()) < 30:
        raise ValueError('杜加流量率定期不足30个有效日')
    path = folder / 'duja-rescore.jsonl'
    prior = _rows(path)
    seen = {row['candidateId'] for row in prior}
    initial = request['parameters']
    total = max(1, int(read_json(source_path.parent / 'search-progress.json', {}).get('traceCount', 34165)))
    count = 0
    eligible_count = sum(bool(r.get('eligible')) for r in prior)
    effective_workers = _rescore_worker_count(evaluator, workers)
    if type(block_size) != int or not 1 <= block_size <= 128:
        raise ValueError('杜加复评候选块大小须为1—128')
    # Serial test doubles must be interruptible after each candidate.
    block_size = block_size if effective_workers > 1 else 1
    worker_evaluators = ([_rescore_worker(evaluator) for _ in range(effective_workers)]
                         if effective_workers > 1 else [evaluator])
    last_reported = len(seen)
    pool = ThreadPoolExecutor(max_workers=effective_workers) if effective_workers > 1 else None
    with source_path.open(encoding='utf-8') as trace, (pool if pool else nullcontext()):
        exhausted = False
        while not exhausted:
            if cancelled():
                raise InterruptedError('已停止杜加候选复评，已完成结果可接续')
            groups = [[] for _ in worker_evaluators]
            pending = set()
            for group in groups:
                while len(group) < block_size:
                    line = trace.readline()
                    if not line:
                        exhausted = True
                        break
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    count += 1
                    candidate = _candidate(initial, row['parameters'], selected, bounds)
                    if candidate is None:
                        continue
                    key = digest({k: candidate[k] for k in selected})
                    if key in seen or key in pending:
                        continue
                    pending.add(key)
                    group.append((count, row, candidate, key))
                if exhausted:
                    break
            if not any(groups):
                continue
            if pool:
                futures = [pool.submit(_score_block, worker, group, mask, selected, constraints)
                           for worker, group in zip(worker_evaluators, groups)]
                results = [future.result() for future in futures]
            else:
                results = [_score_block(worker_evaluators[0], groups[0], mask, selected, constraints)]
            error = None
            written_count = count
            completed_batch = []
            for block, failure in results:
                completed_batch.extend(block)
                if failure is not None:
                    error = failure
                    break
            _append_rescore_batch(path, [result for _, result in completed_batch])
            for source_count, result in completed_batch:
                written_count = source_count
                seen.add(result['candidateId'])
                eligible_count += int(result['eligible'])
            if len(seen)-last_reported >= 25 or exhausted or cancelled() or error:
                write_json(folder / 'search-progress.json', {'phase': 'duja-rescore',
                    'evaluations': len(seen), 'sourceCandidates': written_count, 'sourceTotal': total,
                    'eligible': eligible_count, 'workers': effective_workers,
                    'message': '杜加候选重新评价'})
                progress(min(31., 31.*written_count/total),
                         '杜加候选重新评价 · %d/%d' % (written_count, total))
                last_reported = len(seen)
            if error is not None:
                raise error
            # In-flight blocks are always drained and appended in order before
            # honouring cancellation; the next resume skips all saved vectors.
            if cancelled():
                raise InterruptedError('已停止杜加候选复评，已完成结果可接续')
        # Read through EOF: no earlier score survives a changed warmup.
    records = _rows(path)
    eligible = [row for row in records if row['eligible']]
    if not eligible:
        raise ValueError('新预热条件下没有满足杜加流量门限的参考候选')
    return records, eligible, mask


def choose_candidates(records, selected, bounds, limit=256):
    """Keep 64 best flow fits, then farthest-point diversity in parameter space."""
    ranked = sorted(records, key=lambda r: (r['flowObjective'], r['candidateId']))
    count = min(limit, len(ranked))
    best_count = min(64, count)
    chosen = list(range(best_count))
    if count > best_count:
        box = np.asarray([bounds[k] for k in selected], dtype=float)
        matrix = np.asarray([[r['parameters'][k] for k in selected] for r in ranked], dtype=float)
        matrix = (matrix-box[:, 0])/(box[:, 1]-box[:, 0])
        distance = np.full(len(ranked), np.inf)
        for index in chosen:
            distance = np.minimum(distance, np.sum((matrix-matrix[index])**2, axis=1))
        distance[chosen] = -1.
        while len(chosen) < count:
            index = int(np.argmax(distance))
            chosen.append(index)
            distance = np.minimum(distance, np.sum((matrix-matrix[index])**2, axis=1))
            distance[chosen] = -1.
    return [ranked[i] for i in chosen]


def _serializable_daily(frame):
    keys = ('duja', 'zajia', 'zagen', 'boqu', 'ali', 'fringe', 'total', 'lakeLevel',
            'lakeRainMm', 'lakeEvaporationMm', 'lakeDeltaVolumeM3', 'lakeInflowM3',
            'lakePrecipitationM3', 'lakeEvaporationM3', 'slope')
    return {'dateNs': frame.index.asi8, **{key: frame[key].to_numpy(dtype=float) for key in keys}}


def _write_series(folder, candidate_id, frame):
    output = folder / '候选日过程'
    output.mkdir(parents=True, exist_ok=True)
    # A fresh exclusive name preserves any interrupted earlier attempt.
    path = output / (candidate_id[:20] + '-' + uuid.uuid4().hex[:12] + '.npz')
    with path.open('xb') as stream:
        np.savez_compressed(stream, **_serializable_daily(frame))
        stream.flush()
    return {'file': str(path.relative_to(folder)).replace('\\', '/'), 'sha256': sha_file(path)}


def _read_series(folder, descriptor, observations):
    path = within(folder, descriptor['file'])
    if sha_file(path) != descriptor['sha256']:
        raise ValueError('候选日过程摘要不一致，不能接续')
    with np.load(path, allow_pickle=False) as data:
        values = {key: np.array(data[key]) for key in data.files if key != 'dateNs'}
        dates = pd.to_datetime(np.array(data['dateNs']))
    frame = pd.DataFrame(values, index=dates)
    frame['baseLevel'] = frame['lakeLevel']
    frame['observedLevel'] = pd.Series(list(observations.values()),
                                       index=pd.to_datetime(list(observations))).reindex(frame.index)
    return frame


def _compact_fit(fit):
    return {k: deepcopy(fit[k]) for k in ('value', 'jointScore', 'training', 'boundary', 'trainingPeriod')}


def _target_met(report):
    values = report['training']['scoreComponents']
    return all(value <= 1. for value in values.values())


def _fast_parity(db, request, source, basin, candidates, folder, cancelled):
    path = folder / 'fast-basin-parity.json'
    found = read_json(path)
    if found:
        return found
    proof = []
    for candidate in candidates:
        if cancelled():
            raise InterruptedError('已停止候选计算')
        fast = basin.daily(candidate, cancelled)
        reference = simulate(db, {**request, 'parameters': candidate, '_metricsOnly': True},
                             prepared=source, cancelled=cancelled)
        full = pd.DataFrame(reference['daily'])
        full.index = pd.to_datetime(full.date)
        errors = {}
        for key in ('duja', 'zajia', 'zagen', 'boqu', 'ali', 'fringe', 'total', 'lakeLevel'):
            a, b = full[key].reindex(fast.index).to_numpy(dtype=float), fast[key].to_numpy(dtype=float)
            if not np.array_equal(np.isfinite(a), np.isfinite(b)):
                raise ValueError('快速与完整流域评价的有效日期不一致：' + key)
            mask = np.isfinite(a)
            error = float(np.max(np.abs(a[mask]-b[mask]))) if mask.any() else 0.
            if error > 1e-7:
                raise ValueError('快速与完整流域评价不一致：%s %.9g' % (key, error))
            errors[key] = error
        proof.append({'parameters': candidate, 'maximumDailyDifference': errors})
    result = {'modelImplementation': request['_modelIdentity'],
              'warmupStart': request['start'], 'comparisons': proof}
    write_json(path, result)
    return result


def _evaluate_candidate(record, request, basin, train_window, folder, cached, cancelled):
    identifier = record['candidateId']
    if identifier in cached:
        return cached[identifier]
    candidate = _candidate(request['parameters'], record['parameters'],
                           request['search']['keys'], request['search']['bounds'])
    if candidate is None:
        raise ValueError('候选参数超出当前范围')
    frame = basin.daily(candidate, cancelled)
    bounds = request['search']['bounds']['lakeCoefficient']
    fit = joint_fit(frame, candidate['lakeCoefficient'], bounds, train_window, basin.initial)
    result = {'candidateId': identifier, 'parameters': record['parameters'], 'flow': record['flow'],
              'flowObjective': record['flowObjective'], 'sourceEvaluation': record.get('sourceEvaluation'),
              'trainingLake': _compact_fit(fit), 'series': _write_series(folder, identifier, frame)}
    _append(folder / 'lake-candidates.jsonl', result)
    cached[identifier] = result
    return result


def _refine(evaluator, basin, request, mask, train_window, folder, records, cached, progress, cancelled):
    """Three deterministic DE starts with a single total candidate budget."""
    options = request['twoStage']
    budget = options['jointBudget']
    keys, bounds = request['search']['keys'], request['search']['bounds']
    box = np.asarray([bounds[k] for k in keys], dtype=float)
    ranked = sorted(records, key=lambda r: (r['trainingLake']['jointScore'], r['candidateId']))
    if not ranked or _target_met(ranked[0]['trainingLake']) or budget == 0:
        reached = bool(ranked and _target_met(ranked[0]['trainingLake']))
        return {'evaluations': 0, 'converged': False, 'trainingTargetReached': reached,
                'termination': 'training-target-reached' if reached else 'no-budget'}
    path = folder / 'refinement-state.json'
    state = read_json(path)
    if not state:
        state = {'seedIndex': 0, 'evaluations': 0, 'starts': [], 'active': None,
                 'createdAt': now(), 'bestCandidateId': ranked[0]['candidateId']}
        write_json(path, state)
    seeds = options['seeds']
    constraints = options['flowConstraints']
    while state['seedIndex'] < len(seeds):
        if cancelled():
            raise InterruptedError('已停止联合细化，种群和随机状态已保存')
        seed = seeds[state['seedIndex']]
        if state['active'] is None:
            remaining = budget - state['evaluations']
            quota = remaining // (len(seeds) - state['seedIndex'])
            if quota <= 0:
                break
            rng = np.random.default_rng(seed)
            ranked = sorted(cached.values(), key=lambda r: (r['trainingLake']['jointScore'], r['candidateId']))
            population_size = min(len(ranked), max(12, 2*len(keys)))
            if population_size < 4:
                state['starts'].append({'seed': seed, 'used': 0, 'termination': 'insufficient-population'})
                state['seedIndex'] += 1
                write_json(path, state)
                continue
            # Preserve the best vector, then vary the remaining starts deterministically.
            indexes = [0] + list(rng.choice(np.arange(1, len(ranked)), population_size-1, replace=False))
            pop = [ranked[int(i)] for i in indexes]
            state['active'] = {'seed': seed, 'quota': quota, 'used': 0, 'slot': 0,
                'generation': 0, 'generationBest': [], 'pending': None,
                'population': [{k: row['parameters'][k] for k in keys} for row in pop],
                'candidateIds': [row['candidateId'] for row in pop],
                'scores': [row['trainingLake']['jointScore'] for row in pop],
                'rng': rng.bit_generator.state, 'proposals': 0, 'converged': False,
                'termination': 'budget-exhausted'}
            write_json(path, state)
        active = state['active']
        size = len(active['population'])
        while active['used'] < active['quota']:
            if cancelled():
                raise InterruptedError('已停止联合细化，种群和随机状态已保存')
            pending = active['pending']
            if pending is None:
                rng = np.random.default_rng()
                rng.bit_generator.state = active['rng']
                pop = np.asarray([[v[k] for k in keys] for v in active['population']])
                slot = active['slot']
                choices = [i for i in range(size) if i != slot]
                a, b, c = rng.choice(choices, 3, replace=False)
                mutant = np.clip(pop[a] + rng.uniform(.5, .95)*(pop[b]-pop[c]), box[:, 0], box[:, 1])
                cross = rng.random(len(keys)) < .85
                cross[rng.integers(len(keys))] = True
                vector = np.where(cross, mutant, pop[slot])
                pending = {'slot': slot, 'parameters': dict(zip(keys, map(float, vector)))}
                active['pending'] = pending
                active['rng'] = rng.bit_generator.state
                active['proposals'] += 1
                write_json(path, state)
            candidate = _candidate(request['parameters'], pending['parameters'], keys, bounds)
            metric = None
            if candidate is None:
                active['pending'] = None
                if active['proposals'] >= max(100, active['quota']*100):
                    active['termination'] = 'constraint-proposal-limit'
                    break
                write_json(path, state)
                continue
            identifier = digest({k: candidate[k] for k in keys})
            objective, metric = _flow(evaluator, candidate, mask)
            result = None
            if _eligible(metric, constraints):
                record = {'candidateId': identifier, 'parameters': {k: candidate[k] for k in keys},
                          'flow': metric, 'flowObjective': objective, 'sourceEvaluation': None}
                result = _evaluate_candidate(record, request, basin, train_window, folder, cached, cancelled)
                if result['trainingLake']['jointScore'] < active['scores'][pending['slot']]:
                    slot = pending['slot']
                    active['population'][slot] = pending['parameters']
                    active['candidateIds'][slot] = identifier
                    active['scores'][slot] = result['trainingLake']['jointScore']
                if result['trainingLake']['jointScore'] < cached[state['bestCandidateId']]['trainingLake']['jointScore']:
                    state['bestCandidateId'] = identifier
            active['slot'] = (pending['slot'] + 1) % size
            active['used'] += 1
            state['evaluations'] += 1
            if active['slot'] == 0:
                active['generation'] += 1
                active['generationBest'].append(min(active['scores']))
                history = active['generationBest']
                spread = float(np.std(active['scores']))
                stagnant = len(history) >= 20 and history[-20]-history[-1] <= 1e-5*max(1., abs(history[-20]))
                if spread <= 1e-6*max(1., abs(float(np.mean(active['scores'])))) or (
                        stagnant and spread <= 1e-3*max(1., abs(history[-1]))):
                    active['converged'] = True
                    active['termination'] = 'converged'
            trace = {'evaluation': state['evaluations'], 'stage': 'joint', 'seed': seed,
                     'candidateId': identifier, 'parameters': pending['parameters'], 'flow': metric,
                     'eligible': bool(result), 'objective': result['trainingLake']['jointScore'] if result else None,
                     'bestObjective': cached[state['bestCandidateId']]['trainingLake']['jointScore']}
            _append(folder / 'search-trace.jsonl', trace)
            active['pending'] = None
            write_json(path, state)
            if state['evaluations'] % 10 == 0 or active['converged']:
                write_json(folder / 'search-progress.json', {'phase': 'joint-refinement',
                    'evaluations': state['evaluations'], 'maxEvaluations': budget,
                    'bestObjective': trace['bestObjective'], 'currentStage': 'joint',
                    'traceCount': state['evaluations'], 'message': '流量约束下的湖位筛选'})
                progress(62. + 18.*state['evaluations']/max(1, budget),
                         '湖位联合筛选 · %d/%d' % (state['evaluations'], budget))
            if active['converged'] or _target_met(cached[state['bestCandidateId']]['trainingLake']):
                if _target_met(cached[state['bestCandidateId']]['trainingLake']):
                    active['termination'] = 'training-target-reached'
                break
        state['starts'].append({'seed': seed, 'evaluations': active['used'], 'budget': active['quota'],
            'converged': active['converged'], 'termination': active['termination'],
            'generations': active['generation'], 'population': deepcopy(active['population']),
            'candidateIds': list(active['candidateIds']), 'scores': list(active['scores']),
            'rng': deepcopy(active['rng'])})
        done = active['termination'] == 'training-target-reached'
        state['active'] = None
        state['seedIndex'] += 1
        write_json(path, state)
        if done:
            break
    starts = state['starts']
    return {'evaluations': state['evaluations'], 'starts': starts,
            'converged': bool(starts) and all(start['converged'] for start in starts),
            'termination': starts[-1]['termination'] if starts else 'no-budget'}


def _baseline_comparison(db, identifier, final_lake, window, constraints, flow):
    result = {'eligible': False, 'nse': flow.get('nse'), 'pbias': flow.get('pbias'),
              'lakeRmseImproved': False, 'lakeScoreImproved': False, 'baselineTaskId': identifier}
    if not identifier:
        result['reason'] = '没有冻结当前采用成果'
        return result
    try:
        task = db.task(identifier)
        if task.get('status') != 'succeeded' or not (task.get('result') or {}).get('daily'):
            raise ValueError('当前成果没有可比较的湖位日过程')
        frame = pd.DataFrame(task['result']['daily'])
        frame.index = pd.to_datetime(frame.date)
        frame['baseLevel'] = frame['lakeLevel']
        frame['slope'] = 0.
        baseline = joint_assess(frame, 0., 0., window)
    except (KeyError, ValueError, TypeError) as error:
        result['reason'] = str(error)
        return result
    result.update(baselineLakeRmse=baseline['rmse'], baselineLakeScore=baseline['score'],
                  lakeRmse=final_lake['training']['rmse'], lakeScore=final_lake['jointScore'],
                  lakeRmseImproved=final_lake['training']['rmse'] < baseline['rmse'],
                  lakeScoreImproved=final_lake['jointScore'] < baseline['score'])
    result['eligible'] = bool(_eligible(flow, constraints) and result['lakeRmseImproved'] and result['lakeScoreImproved'])
    return result


def _yearly_flow(evaluator, candidate, periods):
    daily = evaluator.daily(candidate)
    result = {}
    for label, start, end in [('training', periods['trainingStart'], periods['trainingEnd']),
                              ('evaluation', periods['evaluationStart'], periods['evaluationEnd'])]:
        mask = evaluator.mask(start, end)
        result[label] = array_metrics(evaluator.observed[mask], daily[mask]) if mask.any() else None
    mask = evaluator.mask(min(periods['trainingStart'], periods['evaluationStart']),
                          max(periods['trainingEnd'], periods['evaluationEnd']))
    result['all'] = array_metrics(evaluator.observed[mask], daily[mask])
    return result


def _water_budget(frame, coefficient, baseline_coefficient, anchor, lake_area):
    """Monthly and annual complete-day flux totals after the anchor date."""
    data = frame.loc[frame.index > pd.Timestamp(anchor['time']).normalize()].copy()
    old_evap = data['lakeEvaporationM3'].to_numpy(dtype=float)
    if baseline_coefficient == 0:
        raise ValueError('湖面蒸发转换基准不能为零')
    new_evap = old_evap * coefficient/baseline_coefficient
    data['lakeEvaporationM3'] = new_evap
    data['lakeDeltaVolumeM3'] = data['lakeInflowM3'] + data['lakePrecipitationM3'] - new_evap
    keys = ['lakeInflowM3', 'lakePrecipitationM3', 'lakeEvaporationM3', 'lakeDeltaVolumeM3']
    def grouped(freq):
        sums = data[keys].resample(freq).sum(min_count=1)
        counts = data['total'].resample(freq).count()
        return [{'period': t.strftime('%Y-%m') if freq == 'MS' else str(t.year),
                 'completeDays': int(counts.loc[t]), **{k: float(row[k]) for k in keys}}
                for t, row in sums.iterrows() if counts.loc[t] and np.isfinite(row.to_numpy()).all()]
    return {'lakeAreaKm2': float(lake_area)/1e6, 'anchorDayExcluded': pd.Timestamp(anchor['time']).date().isoformat(),
            'basis': '北京时间完整日；起算日为部分日，未计入本表',
            'monthly': grouped('MS'), 'yearly': grouped('YS')}


def run_selection(db, request, source, folder, progress, cancelled):
    """Run candidate rescoring, lake training, time check and all-data selection."""
    folder.mkdir(parents=True, exist_ok=True)
    options = request['twoStage']
    rescore_workers = options.get('rescoreWorkers', DEFAULT_RESCORE_WORKERS)
    if type(rescore_workers) != int or not 1 <= rescore_workers <= 3:
        raise ValueError('杜加复评并行数须为1—3')
    task, trace_path = candidate_source(db, options['candidateSourceTaskId'])
    trace_sha = sha_file(trace_path)
    if trace_sha != request.get('_candidateSourceTraceSHA256'):
        raise ValueError('参考候选轨迹已变化，请重新提交率定')
    periods = request['periods']
    lake_periods = options['lakePeriods']
    observation = request['_observations']['level']
    anchor = request.get('_fixedInitialization')
    if not anchor:
        raise ValueError('湖位筛选需要在任务提交时冻结起算水位')
    train_window = freeze_window(observation, lake_periods['trainingStart'], lake_periods['trainingEnd'], anchor)
    test_window = freeze_window(observation, lake_periods['evaluationStart'], lake_periods['evaluationEnd'], anchor)
    all_window = freeze_window(observation, lake_periods['trainingStart'], lake_periods['evaluationEnd'], anchor)
    identity = digest({'sourceTaskId': options['candidateSourceTaskId'], 'sourceTraceSHA256': trace_sha,
        'inputId': request['inputId'], 'spatialVersion': request['spatialVersion'], 'model': request['_modelIdentity'],
        'start': request['start'], 'end': request['end'], 'initialHydrology': request.get('initialHydrology') or {},
        'initialParameters': request['parameters'], 'search': request['search'], 'options': options,
        'rescoreWorkers': rescore_workers, 'rescoreBlockSize': RESCORE_BLOCK_SIZE,
        'flowObservations': request['_observations']['flow'], 'trainingLake': train_window,
        'testingLake': test_window, 'allLake': all_window, 'anchor': anchor,
        'implementationSHA256': sha_file(__file__),
        'basinImplementationSHA256': sha_file(Path(__file__).with_name('fast_basin_calibration.py')),
        'lakeImplementationSHA256': sha_file(Path(__file__).with_name('lake_calibration.py'))})
    identity_path = folder / 'identity.json'
    prior = read_json(identity_path)
    if prior and prior['identity'] != identity:
        raise ValueError('湖位筛选的输入、参数或观测已改变，不能接续旧状态')
    if not prior:
        write_json(identity_path, {'identity': identity, 'candidateSourceTaskId': options['candidateSourceTaskId'],
            'candidateSourceTraceSHA256': trace_sha, 'trainingLake': train_window,
            'testingLake': test_window, 'allLake': all_window, 'createdAt': now()})
    result_path = folder / 'selection-result.json'
    completed = read_json(result_path)
    if completed:
        if completed.get('identity') != identity:
            raise ValueError('湖位筛选结果身份不一致')
        return completed
    evaluator = DujaEvaluator(source, request, end=max(periods['trainingEnd'], periods['evaluationEnd']))
    rescored, eligible, mask = rescore(evaluator, request, trace_path, folder, progress, cancelled,
                                      workers=rescore_workers)
    selected = choose_candidates(eligible, request['search']['keys'], request['search']['bounds'], options['candidateLimit'])
    write_json(folder / 'candidate-pool.json', {'identity': identity,
        'totalRescored': len(rescored), 'eligible': len(eligible), 'selected': len(selected),
        'flowBestCount': min(64, len(selected)), 'candidateIds': [r['candidateId'] for r in selected],
        'flowConstraints': options['flowConstraints']})
    basin = BasinEvaluator(source, request)
    candidate_values = [_candidate(request['parameters'], row['parameters'], request['search']['keys'],
                                   request['search']['bounds']) for row in (selected[0], selected[-1])]
    parity = _fast_parity(db, request, source, basin, candidate_values, folder, cancelled)
    cached = {row['candidateId']: row for row in _rows(folder / 'lake-candidates.jsonl')}
    for index, row in enumerate(selected):
        if cancelled():
            raise InterruptedError('已停止湖位候选筛选，已完成候选可接续')
        _evaluate_candidate(row, request, basin, train_window, folder, cached, cancelled)
        if (index+1) % 5 == 0 or index+1 == len(selected):
            write_json(folder / 'search-progress.json', {'phase': 'lake-candidate-screening',
                'evaluations': index+1, 'maxEvaluations': len(selected),
                'bestObjective': min(r['trainingLake']['jointScore'] for r in cached.values()),
                'message': '全流域来水与湖位筛选'})
            progress(31. + 31.*(index+1)/len(selected), '湖位候选筛选 · %d/%d' % (index+1, len(selected)))
    refinement = _refine(evaluator, basin, request, mask, train_window, folder,
                         list(cached.values()), cached, progress, cancelled)
    training_best = min(cached.values(), key=lambda r: (r['trainingLake']['jointScore'], r['candidateId']))
    training_frame = _read_series(folder, training_best['series'], observation)
    training_fit = joint_fit(training_frame, request['parameters']['lakeCoefficient'],
                             request['search']['bounds']['lakeCoefficient'], train_window, anchor)
    from .lake_calibration import assess as conventional_assess
    training_fit['validation'] = {**conventional_assess(training_frame, training_fit['value'],
        request['parameters']['lakeCoefficient'], test_window['start'], test_window['end'], anchor),
        **joint_assess(training_frame, training_fit['value'], request['parameters']['lakeCoefficient'], test_window)}
    training_fit['validationPeriod'] = {'start': test_window['start'], 'end': test_window['end']}
    all_path = folder / 'all-lake-fits.jsonl'
    if options['finalRefit']:
        all_records = {r['candidateId']: r for r in _rows(all_path)}
        for index, record in enumerate(cached.values()):
            if cancelled():
                raise InterruptedError('已停止全部湖位资料拟合，已完成候选可接续')
            if record['candidateId'] not in all_records:
                frame = _read_series(folder, record['series'], observation)
                fit = joint_fit(frame, request['parameters']['lakeCoefficient'],
                                request['search']['bounds']['lakeCoefficient'], all_window, anchor)
                row = {'candidateId': record['candidateId'], 'fit': _compact_fit(fit)}
                _append(all_path, row)
                all_records[record['candidateId']] = row
            if (index+1) % 20 == 0 or index+1 == len(cached):
                progress(80. + 2.*(index+1)/len(cached), '全部湖位资料拟合 · %d/%d' % (index+1, len(cached)))
        final_record = min(all_records.values(), key=lambda r: (r['fit']['jointScore'], r['candidateId']))
        final_best = cached[final_record['candidateId']]
        final_frame = _read_series(folder, final_best['series'], observation)
        final_fit = joint_fit(final_frame, request['parameters']['lakeCoefficient'],
                              request['search']['bounds']['lakeCoefficient'], all_window, anchor)
        all_evaluation = final_fit['training']
    else:
        final_best, final_frame, final_fit = training_best, training_frame, deepcopy(training_fit)
        all_evaluation = joint_assess(final_frame, final_fit['value'], request['parameters']['lakeCoefficient'], all_window)
    final_parameters = {**request['parameters'], **final_best['parameters'], 'lakeCoefficient': final_fit['value']}
    flow = _yearly_flow(evaluator, final_parameters, periods)
    comparison_fit = {'training': all_evaluation, 'jointScore': all_evaluation['score']}
    comparison = _baseline_comparison(db, options.get('baselineTaskId'), comparison_fit, all_window,
                                      options['flowConstraints'], flow['all'])
    if not options['finalRefit']:
        comparison['eligible'] = False
        comparison['reason'] = '未启用全部湖位资料拟合，保留时间检验结果，不自动采用'
    water_budget = _water_budget(final_frame, final_fit['value'], request['parameters']['lakeCoefficient'],
                                 anchor, basin.lake_area)
    write_json(folder / 'water-budget.json', water_budget)
    source_workflow = (task.get('result') or {}).get('calibrationWorkflow') or {}
    historical_splits = [r for r in source_workflow.get('flowFits', []) if r.get('validation')]
    summary = {'identity': identity, 'selectionMode': 'flow-constrained-lake',
        'candidateSourceTaskId': options['candidateSourceTaskId'], 'candidateSourceTraceSHA256': trace_sha,
        'flowConstraints': options['flowConstraints'], 'selectedParameters': list(request['search']['keys']),
        'parameters': final_parameters, 'initialParameters': deepcopy(request['parameters']),
        'bounds': deepcopy(request['search']['bounds']), 'seeds': list(options['seeds']),
        'scoreVersion': SCORE_VERSION, 'totalRescored': len(rescored), 'eligibleRescored': len(eligible),
        'rescoreWorkers': _rescore_worker_count(evaluator, rescore_workers),
        'rescoreBlockSize': RESCORE_BLOCK_SIZE,
        'selectedForBasin': len(selected), 'basinCandidatesEvaluated': len(cached),
        'refinement': refinement, 'trainingCandidateId': training_best['candidateId'],
        'finalCandidateId': final_best['candidateId'], 'trainingSeries': training_best['series'],
        'finalSeries': final_best['series'], 'trainingFit': training_fit,
        'trainingParameters': {**request['parameters'], **training_best['parameters'], 'lakeCoefficient': training_fit['value']},
        'finalFit': final_fit, 'allDataEvaluation': all_evaluation,
        'flowMetrics': flow, 'flowObjective': final_best['flowObjective'], 'priorFlowSplitChecks': historical_splits,
        'priorFlowSplitConditions': {'sourceTaskId': options['candidateSourceTaskId'],
            'note': '原分年率定记录保留原预热条件；本轮共享参数的分年指标是拟合评价'},
        'trainingUsesValidationLake': False, 'finalUsesAllLakeObservations': bool(options['finalRefit']),
        'flowParameterSelectionUsesLakeLevel': True, 'adoptionComparison': comparison,
        'fastParity': parity, 'waterBudgetFile': 'water-budget.json',
        'sourceTrace': 'duja-rescore.jsonl', 'candidateTrace': 'lake-candidates.jsonl',
        'refinementTrace': 'search-trace.jsonl', 'allLakeTrace': 'all-lake-fits.jsonl'}
    write_json(result_path, summary)
    return summary


def verify_formal_result(selection, request, baseline, folder):
    """The actually selected vector must match the normal full simulation."""
    fast = _read_series(folder, selection['finalSeries'], request['_observations']['level'])
    full = pd.DataFrame(baseline['daily'])
    full.index = pd.to_datetime(full.date)
    expected_level = (fast['lakeLevel'] +
        (selection['parameters']['lakeCoefficient']-request['parameters']['lakeCoefficient'])*fast['slope'])
    proof = {}
    for key in ('duja', 'zajia', 'zagen', 'boqu', 'ali', 'fringe', 'total', 'lakeLevel'):
        a = full[key].reindex(fast.index).to_numpy(dtype=float)
        b = (expected_level if key == 'lakeLevel' else fast[key]).to_numpy(dtype=float)
        if not np.array_equal(np.isfinite(a), np.isfinite(b)):
            raise ValueError('正式成果与候选有效日期不一致：' + key)
        valid = np.isfinite(a)
        error = float(np.max(np.abs(a[valid]-b[valid]))) if valid.any() else 0.
        if error > 1e-7:
            raise ValueError('正式成果与候选数值不一致：%s %.9g' % (key, error))
        proof[key] = error
    write_json(folder / 'selected-formal-parity.json', {'candidateId': selection['finalCandidateId'],
               'maximumDailyDifference': proof})
    return proof
