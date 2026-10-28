"""Staged Duja discharge calibration with budgeted local response checks.

The observations and date mask used to choose parameters are fixed from the
training period. Lake level is reported only after the final simulation.
"""
from copy import deepcopy
import math

import numpy as np
import pandas as pd

from .calibration_config import LAND_KEYS, SNOW_KEYS, ROUTING_KEYS, FLOW_SELECTABLE_KEYS, PROFILES, flow_objective
from .common import digest, sha_file
from .model_core import PARAMETER_DEFINITIONS, parameters
from .simulation import metrics


def _frame(result):
    frame = pd.DataFrame(result['daily'])
    if 'date' not in frame:
        raise ValueError('计算结果缺少日期，无法评价率定目标')
    frame.index = pd.to_datetime(frame['date'])
    if frame.index.has_duplicates:
        raise ValueError('计算结果包含重复日期，无法评价率定目标')
    return frame.sort_index()


def _pair(frame, target, start, end, initial=None):
    obs, sim = ('observedFlow', 'duja') if target == 'flow' else ('observedLevel', 'lakeLevel')
    frame = frame.loc[(frame.index >= start) & (frame.index < end)]
    if target == 'lake' and initial:
        frame = frame.loc[frame.index != pd.Timestamp(initial['time']).normalize()]
    if obs not in frame or sim not in frame:
        return pd.DataFrame(columns=['obs', 'sim'], index=pd.DatetimeIndex([]))
    pairs = pd.DataFrame({'obs': pd.to_numeric(frame[obs], errors='coerce'),
                          'sim': pd.to_numeric(frame[sim], errors='coerce')}, index=frame.index)
    return pairs.loc[np.isfinite(pairs['obs']) & np.isfinite(pairs['sim'])]


def _assessment(frame, target, start, end, initial):
    pair = _pair(frame, target, start, end, initial)
    result = metrics(pair.obs, pair.sim)
    result['countByYear'] = {str(y): int(n) for y, n in pair.groupby(pair.index.year).size().items()}
    if target == 'lake' and len(pair) > 1:
        error = pair.sim - pair.obs
        result['firstToLastErrorChangeM'] = float(error.iloc[-1] - error.iloc[0])
    return result


def _sensitivity(columns):
    """SVD and Pearson correlation of dimensionless one-sided derivatives."""
    keys = list(columns)
    if not keys:
        return {'keys': [], 'singularValues': [], 'rank': 0, 'correlation': []}
    matrix = np.column_stack([columns[key] for key in keys])
    singular = np.linalg.svd(matrix, compute_uv=False)
    correlation = []
    for i in range(len(keys)):
        row = []
        for j in range(len(keys)):
            a, b = matrix[:, i], matrix[:, j]
            if np.std(a) <= 1e-14 or np.std(b) <= 1e-14:
                row.append(None)
            else:
                row.append(float(np.clip(np.corrcoef(a, b)[0, 1], -1, 1)))
        correlation.append(row)
    return {'keys': keys, 'singularValues': [float(x) for x in singular],
            'rank': int(np.linalg.matrix_rank(matrix)), 'correlation': correlation,
            'interpretation': '固定率定日期的一次局部扰动诊断，不证明全局可辨识性。'}


def run(db, request, folder, progress, cancelled, *, prepare, simulate, write_json):
    from .research_schemes import resolved_initialization

    search, periods = request['search'], request['periods']
    if request.get('target') != 'duja' or search.get('scope') != 'duja':
        raise ValueError('杜加流量率定必须选择杜加控制区参数')
    selected = list(search.get('keys', []))
    if not selected or len(set(selected)) != len(selected) or set(selected) - set(FLOW_SELECTABLE_KEYS):
        raise ValueError('杜加流量率定包含不可选参数或重复参数')
    seeds = search.get('seeds', [search.get('seed', 42)])
    if (not isinstance(seeds, list) or not 1 <= len(seeds) <= 4 or
            any(type(seed) != int or not 0 <= seed < 2**32 for seed in seeds) or len(set(seeds)) != len(seeds)):
        raise ValueError('率定随机起点须为1—4个不重复的非负整数')
    stages = [(name, [k for k in selected if k in group]) for name, group in
              (('runoff', LAND_KEYS), ('snow', SNOW_KEYS), ('routing', ROUTING_KEYS))]
    stages = [(name, keys) for name, keys in stages if keys]
    for name, _ in stages:
        budget = search['budgets'].get(name, 120 if name == 'snow' else None)
        if type(budget) != int or not 1 <= budget <= 2000:
            raise ValueError('每阶段评价次数须为1—2000的整数')
    transfer_mode = (request.get('parameterTransfer') or {}).get('mode')
    shared = transfer_mode in ('duja-shared', 'duja-area-routing')
    if shared and request.get('partitionParameters'):
        raise ValueError('共享参数传递方案不能包含分区参数覆盖')
    source = prepare(db, request['inputId'], request['spatialVersion'])
    initial = (request['_fixedInitialization'] if '_fixedInitialization' in request else
               resolved_initialization(request, request['_observations'], 'calibration')[0])
    working = deepcopy(request)
    working['_fixedInitialization'] = deepcopy(initial)
    observation = deepcopy(request['_observations'])
    training_start, training_end = pd.Timestamp(periods['trainingStart']), pd.Timestamp(periods['trainingEnd'])
    for key in ('level', 'flow'):
        observation[key] = {t: v for t, v in observation[key].items() if pd.Timestamp(t) < training_end}
    all_trace, records = [], []
    fixed_dates, fixed_observed, observed_scale = None, None, None
    target_summary = None
    objective_weights = deepcopy(PROFILES.get(search.get('objectiveProfile'), {}).get('weights'))
    calibration_implementation_sha256 = sha_file(__file__)

    def search_evidence():
        # Hash ordered date/value pairs, using common.canonical JSON, so the
        # exact training observation mask can be verified from the snapshot.
        pairs = [] if fixed_dates is None else [
            {'date': date.isoformat(), 'observed': float(value)}
            for date, value in zip(fixed_dates, fixed_observed)]
        return {'fixedTrainingDates': [pair['date'] for pair in pairs],
                'fixedTrainingObservationSHA256': digest(pairs),
                'objectiveWeights': objective_weights,
                'calibrationImplementationSHA256': calibration_implementation_sha256}

    def condition(template, candidate, keys):
        value = deepcopy(template)
        candidate = parameters(candidate)
        if shared:
            value['parameters'] = candidate
            value['partitionParameters'] = {}
        else:
            region = deepcopy(value.get('partitionParameters', {}).get('duja', {}))
            region.update({k: candidate[k] for k in keys})
            parameters({**value['parameters'], **region})
            value.setdefault('partitionParameters', {})['duja'] = region
        return value

    for stage_index, (stage_name, stage_keys) in enumerate(stages):
        if cancelled():
            raise InterruptedError('用户停止率定，搜索轨迹已保留')
        budget = search['budgets'].get(stage_name, 120)
        base_global = parameters(working['parameters'])
        baseline = base_global if shared else parameters({**base_global, **working.get('partitionParameters', {}).get('duja', {})})
        bounds = {k: search['bounds'][k] for k in stage_keys}
        definitions = {d['key']: d for d in PARAMETER_DEFINITIONS}
        for key, box in bounds.items():
            if (not isinstance(box, list) or len(box) != 2 or not all(type(x) in (int, float) and np.isfinite(x) for x in box)
                    or box[0] >= box[1] or not box[0] <= baseline[key] <= box[1]
                    or box[0] < definitions[key]['min'] or box[1] > definitions[key]['max']):
                raise ValueError('率定参数的初值或上下界无效：' + key)
        template = {**working, 'end': periods['trainingEnd'], '_observations': observation, '_metricsOnly': True}
        template.pop('evaluationStart', None)
        template.pop('evaluationEnd', None)
        stage_trace, best, best_candidate, baseline_metric = [], math.inf, baseline.copy(), None

        def candidate_for(values):
            return parameters({**baseline, **values})

        def evaluate(values, phase, seed=None):
            nonlocal fixed_dates, fixed_observed, observed_scale, target_summary
            nonlocal best, best_candidate, baseline_metric
            if cancelled():
                raise InterruptedError('用户停止率定，搜索轨迹已保留')
            candidate = candidate_for(values)
            output = simulate(db, condition(template, candidate, stage_keys), prepared=source, cancelled=cancelled)
            frame = _frame(output)
            pair = _pair(frame, 'flow', training_start, training_end)
            if fixed_dates is None:
                fixed_dates, fixed_observed = pair.index, pair.obs.to_numpy(dtype=float)
                observed_scale = float(np.std(fixed_observed)) if len(pair) else 0.
                target_summary = {'count': len(pair), 'countByYear': {str(y): int(n) for y, n in pair.groupby(pair.index.year).size().items()},
                                  'available': len(pair) >= 30, 'usedForObjective': True}
            pair = pair.reindex(fixed_dates)
            if (len(pair) != len(fixed_dates) or not np.isfinite(pair.to_numpy(dtype=float)).all() or
                    not np.array_equal(pair.obs.to_numpy(dtype=float), fixed_observed)):
                raise ValueError('率定候选缺少或改变固定训练流量配对日期，无法比较')
            metric = metrics(pair.obs, pair.sim)
            try:
                objective = flow_objective(metric, search['objectiveProfile'])[0] if len(pair) >= 30 else None
            except ValueError:
                objective = None
            if objective is not None and not np.isfinite(objective):
                objective = None
            if baseline_metric is None:
                baseline_metric = deepcopy(metric)
            if objective is not None and objective < best:
                best, best_candidate = float(objective), candidate.copy()
            item = {'stage': stage_name, 'phase': phase, 'seed': seed,
                    'evaluation': len(stage_trace) + 1, 'objective': float(objective) if objective is not None else None,
                    'bestObjective': float(best) if np.isfinite(best) else None,
                    'parameters': {k: candidate[k] for k in stage_keys}, 'metrics': metric}
            stage_trace.append(item)
            all_trace.append(item)
            write_json(folder / 'search-progress.json', {'stages': records, 'currentStage': stage_name,
                       'trace': all_trace, 'trainingPeriod': {'start': periods['trainingStart'], 'end': periods['trainingEnd']},
                       'targets': {'flow': target_summary}, 'evaluationExcluded': True,
                       'configurationSHA256': request['schemeSnapshot']['configurationSHA256'], **search_evidence()})
            progress((stage_index + len(stage_trace) / budget) / len(stages) * 90,
                     '%s 率定 %d/%d' % (stage_name, len(stage_trace), budget))
            return objective, pair.sim.to_numpy(dtype=float)

        base_score, base_flow = evaluate({}, 'baseline')
        diagnostic = {'trainingPairs': len(fixed_dates), 'countByYear': target_summary['countByYear'],
                      'perturbationFractionOfRange': .05, 'parameters': {}, 'svd': {}}
        starts = []
        if base_score is None:
            target_summary.update(available=False, reason='率定期可靠配对不足30日或流量目标不可评价')
            for key in stage_keys:
                diagnostic['parameters'][key] = {'status': 'not-evaluable', 'reason': target_summary['reason']}
            termination, converged, free = 'no-evaluable-target', False, []
        else:
            # Reserve one actual proposal per seed when possible. Unchecked
            # parameters remain searchable and are never called insensitive.
            diagnostic_limit = budget - min(len(seeds), max(0, budget - len(stage_trace)))
            columns, frozen = {}, set()
            for key in stage_keys:
                if len(stage_trace) >= diagnostic_limit:
                    diagnostic['parameters'][key] = {'status': 'not-tested', 'reason': '评价次数不足，未进行响应检查'}
                    continue
                lo, hi = bounds[key]
                step = (hi - lo) * .05
                alternatives = (min(hi, baseline[key] + step), max(lo, baseline[key] - step))
                trial_value = None
                for v in alternatives:
                    if abs(v - baseline[key]) <= 1e-15:
                        continue
                    try:
                        candidate_for({key: v})
                        trial_value = v
                        break
                    except ValueError:
                        continue
                if trial_value is None:
                    frozen.add(key)
                    diagnostic['parameters'][key] = {'status': 'constraint-blocked', 'reason': '本地扰动不满足参数联合约束'}
                    continue
                _, altered = evaluate({key: trial_value}, 'response')
                delta = altered - base_flow
                maximum = float(np.max(np.abs(delta))) if len(delta) else 0.
                tolerance = 1e-10 * max(1., float(np.max(np.abs(base_flow))) if len(base_flow) else 0.)
                normalized_step = (trial_value - baseline[key]) / (hi - lo)
                if maximum <= tolerance:
                    frozen.add(key)
                    diagnostic['parameters'][key] = {'status': 'no-local-response', 'reason': '本期扰动下杜加流量无可分辨变化，暂不搜索',
                                                     'perturbation': trial_value, 'maxFlowChange': maximum, 'tolerance': tolerance}
                else:
                    columns[key] = delta / max(observed_scale, 1e-12) / normalized_step
                    diagnostic['parameters'][key] = {'status': 'responsive', 'perturbation': trial_value,
                                                     'maxFlowChange': maximum, 'tolerance': tolerance}
            diagnostic['svd'] = _sensitivity(columns)
            free = [k for k in stage_keys if k not in frozen]
            remaining = budget - len(stage_trace)
            for index, seed in enumerate(seeds):
                quota = remaining // (len(seeds) - index)
                remaining -= quota
                start_record = {'seed': seed, 'maxEvaluations': quota, 'evaluations': 0,
                                'baselineReused': True, 'converged': False}
                if not free or quota == 0:
                    start_record['termination'] = 'no-free-parameters' if not free else 'no-budget'
                    starts.append(start_record)
                    continue
                rng = np.random.default_rng(seed)
                box = np.array([bounds[k] for k in free], dtype=float)
                vector = np.array([baseline[k] for k in free], dtype=float)
                population, scores = [vector.copy()], [base_score]
                proposals, proposal_limit = 0, max(50, quota * 50)
                size = max(6, len(free) * 2)
                while start_record['evaluations'] < quota and proposals < proposal_limit:
                    proposals += 1
                    if len(population) < size or len(population) < 4:
                        candidate = box[:, 0] + rng.random(len(free)) * (box[:, 1] - box[:, 0])
                        slot = None
                    else:
                        slot = (proposals - 1) % len(population)
                        choices = [j for j in range(len(population)) if j != slot]
                        a, b, c = rng.choice(choices, 3, replace=False)
                        mutant = np.clip(population[a] + .7 * (population[b] - population[c]), box[:, 0], box[:, 1])
                        cross = rng.random(len(free)) < .8
                        cross[rng.integers(len(free))] = True
                        candidate = np.where(cross, mutant, population[slot])
                    updates = dict(zip(free, map(float, candidate)))
                    try:
                        candidate_for(updates)
                    except ValueError:
                        continue
                    value, _ = evaluate(updates, 'search', seed)
                    if value is None:
                        raise ValueError('率定候选的固定训练目标无效')
                    start_record['evaluations'] += 1
                    if slot is None:
                        population.append(candidate)
                        scores.append(value)
                    elif value < scores[slot]:
                        population[slot], scores[slot] = candidate, value
                    if (len(population) >= 4 and start_record['evaluations'] >= max(12, len(population) * 3)
                            and np.std(scores) < 1e-6):
                        start_record['converged'] = True
                        break
                best_slot = int(np.argmin(scores))
                start_record.update({'bestObjective': float(scores[best_slot]),
                                     'bestParameters': dict(zip(free, map(float, population[best_slot]))),
                                     'termination': 'converged' if start_record['converged'] else
                                         'budget-exhausted' if start_record['evaluations'] >= quota else 'constraint-proposal-limit'})
                starts.append(start_record)
                remaining += quota - start_record['evaluations']
            converged = bool(starts) and all(s['converged'] for s in starts)
            termination = ('no-responsive-parameters' if not free else 'converged' if converged else
                           'budget-exhausted' if len(stage_trace) >= budget else
                           'constraint-proposal-limit' if any(s['termination'] == 'constraint-proposal-limit' for s in starts)
                           else 'starts-completed')
        touching = [k for k in stage_keys if min(abs(best_candidate[k] - bounds[k][0]),
                                                  abs(best_candidate[k] - bounds[k][1])) <= 1e-8]
        near = [k for k in stage_keys if min(abs(best_candidate[k] - bounds[k][0]),
                                              abs(best_candidate[k] - bounds[k][1])) <= .01 * (bounds[k][1] - bounds[k][0])]
        record = {'stage': stage_name, 'evaluations': len(stage_trace), 'maxEvaluations': budget,
                  'keys': free, 'selectedParameters': stage_keys, 'fixedParameters': [k for k in stage_keys if k not in free],
                  'bounds': bounds, 'initialParameters': {k: baseline[k] for k in stage_keys},
                  'bestParameters': {k: best_candidate[k] for k in stage_keys}, 'baselineMetrics': baseline_metric,
                  'bestObjective': float(best) if np.isfinite(best) else None, 'converged': converged,
                  'termination': termination, 'boundaryParameters': touching, 'nearBoundaryParameters': near,
                  'diagnostics': diagnostic, 'starts': starts}
        records.append(record)
        if base_score is None:
            break
        working = condition(working, best_candidate, stage_keys)

    if cancelled():
        raise InterruptedError('用户停止率定，搜索轨迹已保留')
    final = simulate(db, {**working, '_metricsOnly': False}, folder,
                     lambda p, m: progress(90 + p * .09, m), cancelled, source)
    frame = _frame(final)
    reports = {label: {target: _assessment(frame, target, pd.Timestamp(start), pd.Timestamp(end), initial)
                       for target in ('flow', 'lake')}
               for label, start, end in (('training', periods['trainingStart'], periods['trainingEnd']),
                                          ('validation', periods['evaluationStart'], periods['evaluationEnd']))}
    lake_training = reports['training']['lake']
    targets = {'flow': target_summary,
               'lake': {'available': lake_training['count'] > 0, 'count': lake_training['count'],
                        'countByYear': lake_training['countByYear'], 'usedForObjective': False,
                        'reason': '湖位仅作独立评价，不参与杜加流量目标'}}
    final['calibration'] = {'algorithm': 'staged-multistart-differential-evolution', 'seed': seeds[0], 'seeds': seeds,
                            'evaluations': len(all_trace), 'trace': all_trace, 'stages': records,
                            'objectiveProfile': search['objectiveProfile'], 'periods': periods,
                            'targets': targets, 'assessments': reports, 'validationExcludedFromSearch': True,
                            'decision': 'candidate-only', 'converged': all(s['converged'] for s in records),
                            'boundaryParameters': sorted({k for s in records for k in s['boundaryParameters']}),
                            'nearBoundaryParameters': sorted({k for s in records for k in s['nearBoundaryParameters']}),
                            'termination': 'no-evaluable-target' if not target_summary['available'] else 'all-stages-completed',
                            'bestObjective': records[-1]['bestObjective'], 'baselineMetrics': records[0]['baselineMetrics'],
                            **search_evidence()}
    final['calibrationWorkflow'] = {'target': 'duja', 'stages': records}
    write_json(folder / 'search-progress.json', {'stages': records, 'trace': all_trace, 'targets': targets,
               'evaluationExcluded': True, 'configurationSHA256': request['schemeSnapshot']['configurationSHA256'],
               **search_evidence()})
    write_json(folder / 'result.json', final)
    return final
