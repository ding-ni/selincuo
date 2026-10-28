"""One continuous run and one parameter set for flow and lake calibration.

Only observations before the end of training can select the lake anchor or
affect the search. The two targets have independent, fixed date masks and
scales, so additional observations of one variable do not increase its weight.
"""
from copy import deepcopy
import math

import numpy as np
import pandas as pd

from .calibration_config import RUNOFF_KEYS, ROUTING_KEYS
from .common import write_json
from .grid_simulation import prepare, run as simulate
from .model_core import parameters
from .simulation import metrics

FLOW_KEYS = set(RUNOFF_KEYS + ROUTING_KEYS)
TARGETS = {'flow': ('observedFlow', 'duja'), 'lake': ('observedLevel', 'lakeLevel')}


def _frame(result):
    frame = pd.DataFrame(result['daily'])
    if 'date' not in frame:
        raise ValueError('计算结果缺少日期，不能评价率定目标')
    frame.index = pd.to_datetime(frame['date'])
    if frame.index.has_duplicates:
        raise ValueError('计算结果包含重复日期，不能评价率定目标')
    return frame.sort_index()


def _slice(frame, start, end, initial=None, lake=False):
    result = frame.loc[(frame.index >= start) & (frame.index < end)]
    if lake and initial:
        result = result.loc[result.index != pd.Timestamp(initial['time']).normalize()]
    return result


def _pairs(frame, target):
    observed, simulated = TARGETS[target]
    if observed not in frame or simulated not in frame:
        return pd.DataFrame(columns=['obs', 'sim'], index=pd.DatetimeIndex([]))
    pair = pd.DataFrame({'obs': pd.to_numeric(frame[observed], errors='coerce'),
                         'sim': pd.to_numeric(frame[simulated], errors='coerce')}, index=frame.index)
    return pair.loc[np.isfinite(pair['obs']) & np.isfinite(pair['sim'])]


def target_masks(frame, periods, initial, config):
    """Fix independent, finite date masks and observed training scales once."""
    start, end = pd.Timestamp(periods['trainingStart']), pd.Timestamp(periods['trainingEnd'])
    output = {}
    for target in TARGETS:
        pair = _pairs(_slice(frame, start, end, initial, target == 'lake'), target)
        scale = float(pair.obs.std(ddof=0)) if len(pair) else None
        reason = ('有效配对少于%d日' % config['minimumPairs'] if len(pair) < config['minimumPairs']
                  else '观测变化太小，不能建立有效评价尺度' if not scale or scale <= config['minimumScale']
                  else None)
        output[target] = {'available': reason is None, 'reason': reason,
                          'count': len(pair), 'scale': scale,
                          'dates': [d.isoformat() for d in pair.index],
                          'countByYear': {str(y): int(n) for y, n in pair.groupby(pair.index.year).size().items()}}
    return output


def score(frame, masks, config):
    """Score only the originally selected dates; missing a date is an error."""
    components, summaries = {}, {}
    for target, info in masks.items():
        if not info['available']:
            continue
        dates = pd.DatetimeIndex(pd.to_datetime(info['dates']))
        pair = _pairs(frame, target).reindex(dates)
        if len(pair) != info['count'] or not np.isfinite(pair.to_numpy(dtype=float)).all():
            raise ValueError(('杜加流量' if target == 'flow' else '主站湖位') + '率定候选缺少固定评价日期，无法比较')
        metric = metrics(pair.obs, pair.sim)
        summaries[target] = metric
        components[target] = metric['rmse'] / info['scale']
    if not components:
        return None, summaries, components
    weight = sum(config['weights'][target] for target in components)
    objective = sum(config['weights'][target] * value for target, value in components.items()) / weight
    return float(objective), summaries, {k: float(v) for k, v in components.items()}


def lake_seed(baseline, probe, mask, baseline_coefficient, probe_coefficient, bounds):
    """Estimate an RMSE-optimal lake coefficient from two real trajectories.

    Only the frozen training dates are used. A subsequent real simulation must
    still evaluate the returned value against the joint objective.
    """
    if not mask['available'] or baseline_coefficient == probe_coefficient:
        return None
    dates = pd.DatetimeIndex(pd.to_datetime(mask['dates']))
    a, b = (_pairs(frame, 'lake').reindex(dates) for frame in (baseline, probe))
    if (len(a) != mask['count'] or len(b) != mask['count'] or
            not np.isfinite(a.to_numpy(dtype=float)).all() or not np.isfinite(b.to_numpy(dtype=float)).all()):
        raise ValueError('湖位探针缺少固定率定日期，无法比较')
    if not np.array_equal(a.obs.to_numpy(), b.obs.to_numpy()):
        raise ValueError('湖位探针观测发生变化，无法比较')
    error = a.sim.to_numpy() - a.obs.to_numpy()
    difference = b.sim.to_numpy() - a.sim.to_numpy()
    norm = float(np.dot(difference, difference))
    if not np.isfinite(norm) or norm <= 1e-24:
        return None
    optimum = baseline_coefficient - (probe_coefficient - baseline_coefficient) * float(np.dot(error, difference)) / norm
    return float(np.clip(optimum, bounds[0], bounds[1])) if np.isfinite(optimum) else None


def _assessment(frame, target, initial, start, end):
    pair = _pairs(_slice(frame, start, end, initial, target == 'lake'), target)
    result = metrics(pair.obs, pair.sim)
    result['countByYear'] = {str(y): int(n) for y, n in pair.groupby(pair.index.year).size().items()}
    if not len(pair):
        return result
    error = pair.sim - pair.obs
    if target == 'lake':
        result['firstToLastErrorChangeM'] = float(error.iloc[-1] - error.iloc[0]) if len(pair) > 1 else None
        seasons = {'冬': (12, 1, 2), '春': (3, 4, 5), '夏': (6, 7, 8), '秋': (9, 10, 11)}
        result['seasonalMeanErrorM'] = {name: float(error[pair.index.month.isin(months)].mean())
                                       for name, months in seasons.items() if pair.index.month.isin(months).any()}
    else:
        peak = pair.obs.idxmax()
        result['observedPeak'] = {'date': peak.date().isoformat(), 'observed': float(pair.loc[peak, 'obs']),
                                  'simulatedSameDay': float(pair.loc[peak, 'sim'])}
    return result


def run(db, request, folder, progress, cancelled):
    from .research_schemes import resolved_initialization

    search, periods = request['search'], request['periods']
    config = search['joint']
    source = prepare(db, request['inputId'], request['spatialVersion'])
    initial = (request['_fixedInitialization'] if '_fixedInitialization' in request else
               resolved_initialization(request, request['_observations'], 'calibration')[0])
    working = deepcopy(request)
    working['_fixedInitialization'] = deepcopy(initial)
    observation = deepcopy(request['_observations'])
    training_end = pd.Timestamp(periods['trainingEnd'])
    for key in ('level', 'flow'):
        observation[key] = {t: v for t, v in observation[key].items() if pd.Timestamp(t) < training_end}
    candidate_template = {**working, 'end': periods['trainingEnd'], '_observations': observation, '_metricsOnly': True}
    candidate_template.pop('evaluationStart', None)
    candidate_template.pop('evaluationEnd', None)
    if cancelled():
        raise InterruptedError('用户停止综合率定，搜索轨迹已保留')
    base_global = parameters(working['parameters'])
    base_duja = parameters({**base_global, **working.get('partitionParameters', {}).get('duja', {})})
    baseline_frame = _frame(simulate(db, candidate_template, prepared=source, cancelled=cancelled))
    masks = target_masks(baseline_frame, periods, initial, config)
    selected = list(search['keys'])
    free = [k for k in selected if (k in FLOW_KEYS and masks['flow']['available']) or
            (k == 'lakeCoefficient' and masks['lake']['available'])]
    fixed = [k for k in selected if k not in free]
    baseline = {k: base_global[k] if k == 'lakeCoefficient' else base_duja[k] for k in selected}
    budget, rng = search['budgets']['integrated'], np.random.default_rng(search['seed'])
    trace, best_vector, best, best_metrics = [], None, math.inf, None
    public_masks = {key: {k: v for k, v in row.items() if k != 'dates'} for key, row in masks.items()}

    def conditions(vector, template):
        value = deepcopy(template)
        updates = dict(zip(free, map(float, vector)))
        global_values = parameters({**base_global, **({'lakeCoefficient': updates['lakeCoefficient']}
                                                     if 'lakeCoefficient' in updates else {})})
        duja_updates = {k: v for k, v in updates.items() if k in FLOW_KEYS}
        region = {**value.get('partitionParameters', {}).get('duja', {}), **duja_updates}
        parameters({**global_values, **region})
        value['parameters'] = global_values
        if duja_updates:
            value.setdefault('partitionParameters', {})['duja'] = region
        return value

    def evaluate(vector, precomputed=None):
        nonlocal best, best_vector, best_metrics
        if cancelled():
            raise InterruptedError('用户停止综合率定，搜索轨迹已保留')
        candidate = conditions(vector, candidate_template)
        frame = precomputed if precomputed is not None else _frame(simulate(db, candidate, prepared=source, cancelled=cancelled))
        objective, found_metrics, components = score(frame, masks, config)
        if objective is None:
            return None
        if objective < best:
            best, best_vector, best_metrics = objective, np.array(vector, dtype=float).copy(), deepcopy(found_metrics)
        item = {'stage': 'integrated', 'evaluation': len(trace) + 1, 'objective': objective,
                'bestObjective': float(best), 'parameters': dict(zip(free, map(float, vector))),
                'metrics': found_metrics, 'components': components}
        trace.append(item)
        write_json(folder / 'search-progress.json', {'stages': [], 'currentStage': 'integrated', 'trace': trace,
                   'targets': public_masks, 'fixedParameters': fixed, 'trainingPeriod': {
                       'start': periods['trainingStart'], 'end': periods['trainingEnd']},
                   'evaluationExcluded': True, 'configurationSHA256': request['schemeSnapshot']['configurationSHA256']})
        progress(len(trace) / budget * 90, '综合率定 %d/%d · 最佳 %.5g' % (len(trace), budget, best))
        return objective

    initial_vector = np.array([baseline[k] for k in free], dtype=float)
    baseline_objective, baseline_metrics, _ = score(baseline_frame, masks, config)
    converged, proposals = False, 0
    seed_info = {'status': 'not-applicable', 'method': 'two-model-trajectories-training-lake-rmse-v1'}
    if free and baseline_objective is not None:
        bounds = np.array([search['bounds'][k] for k in free], dtype=float)
        initial_score = evaluate(initial_vector, baseline_frame)
        population, scores = [initial_vector.copy()], [initial_score]
        if 'lakeCoefficient' in free and masks['lake']['available']:
            if budget < 3:
                seed_info['status'] = 'budget-insufficient'
            else:
                index = free.index('lakeCoefficient')
                lo, hi = bounds[index]
                start = float(initial_vector[index])
                probe_value = float(hi if hi - start >= start - lo else lo)
                probe_vector = initial_vector.copy()
                probe_vector[index] = probe_value
                probe_request = conditions(probe_vector, candidate_template)
                probe_frame = _frame(simulate(db, probe_request, prepared=source, cancelled=cancelled))
                probe_score = evaluate(probe_vector, probe_frame)
                population.append(probe_vector.copy()); scores.append(probe_score)
                estimate = lake_seed(baseline_frame, probe_frame, masks['lake'], start, probe_value, (lo, hi))
                seed_info.update({'baselineCoefficient': start, 'probeCoefficient': probe_value,
                                  'estimatedCoefficient': estimate, 'probeEvaluation': len(trace),
                                  'trainingOnly': True})
                if estimate is None:
                    seed_info['status'] = 'insensitive'
                elif abs(estimate - start) < 1e-12 or abs(estimate - probe_value) < 1e-12:
                    seed_info['status'] = 'matches-evaluated'
                else:
                    seed_vector = initial_vector.copy()
                    seed_vector[index] = estimate
                    seed_score = evaluate(seed_vector)
                    population.append(seed_vector.copy()); scores.append(seed_score)
                    seed_info.update({'status': 'evaluated', 'seedEvaluation': len(trace)})
        size = min(budget, max(6, len(free) * 2))
        while len(population) < size and len(trace) < budget and proposals < budget * 50:
            vector = bounds[:, 0] + rng.random(len(free)) * (bounds[:, 1] - bounds[:, 0])
            proposals += 1
            try:
                conditions(vector, candidate_template)
            except ValueError:
                continue
            population.append(vector)
            scores.append(evaluate(vector))
        while len(trace) < budget and proposals < budget * 50 and population:
            for i in range(len(population)):
                proposals += 1
                if len(population) >= 4:
                    choices = [j for j in range(len(population)) if j != i]
                    a, b, c = rng.choice(choices, 3, replace=False)
                    mutant = np.clip(population[a] + .7 * (population[b] - population[c]), bounds[:, 0], bounds[:, 1])
                    cross = rng.random(len(free)) < .8
                    cross[rng.integers(len(free))] = True
                    vector = np.where(cross, mutant, population[i])
                else:
                    vector = bounds[:, 0] + rng.random(len(free)) * (bounds[:, 1] - bounds[:, 0])
                try:
                    conditions(vector, candidate_template)
                except ValueError:
                    continue
                value = evaluate(vector)
                if value < scores[i]:
                    population[i], scores[i] = vector, value
                if len(trace) >= budget or proposals >= budget * 50:
                    break
            if len(trace) >= max(12, len(population) * 3) and np.std(scores) < 1e-6:
                converged = True
                break
    if cancelled():
        raise InterruptedError('用户停止综合率定，搜索轨迹已保留')
    chosen = best_vector if best_vector is not None else initial_vector
    working = conditions(chosen, working)
    termination = ('no-evaluable-target' if baseline_objective is None else 'no-free-parameters' if not free else
                   'converged' if converged else 'budget-exhausted' if len(trace) >= budget else 'constraint-proposal-limit')
    touching = [k for k, v in zip(free, chosen) if min(abs(v - search['bounds'][k][0]),
                                                       abs(v - search['bounds'][k][1])) < 1e-8]
    stage = {'stage': 'integrated', 'evaluations': len(trace), 'maxEvaluations': budget,
             'keys': free, 'fixedParameters': fixed, 'bounds': {k: search['bounds'][k] for k in free},
             'initialParameters': baseline, 'bestParameters': {**baseline, **dict(zip(free, map(float, chosen)))},
             'baselineMetrics': baseline_metrics, 'bestMetrics': best_metrics,
             'bestObjective': float(best) if best_vector is not None else baseline_objective,
             'converged': converged, 'termination': termination, 'boundaryParameters': touching,
             'targets': public_masks, 'lakeSeed': seed_info}
    write_json(folder / 'search-progress.json', {'stages': [stage], 'trace': trace, 'targets': public_masks,
               'fixedParameters': fixed, 'evaluationExcluded': True,
               'configurationSHA256': request['schemeSnapshot']['configurationSHA256']})
    final = simulate(db, {**working, '_metricsOnly': False}, folder,
                     lambda p, m: progress(90 + p * .09, m), cancelled, source)
    final_frame = _frame(final)
    reports = {label: {target: _assessment(final_frame, target, initial, pd.Timestamp(start), pd.Timestamp(end))
                       for target in TARGETS}
               for label, start, end in (('training', periods['trainingStart'], periods['trainingEnd']),
                                          ('validation', periods['evaluationStart'], periods['evaluationEnd']))}
    final['calibration'] = {'algorithm': 'differential-evolution', 'seed': search['seed'],
                            'evaluations': len(trace), 'trace': trace, 'stages': [stage],
                            'objectiveProfile': config['method'], 'objectiveWeights': config['weights'],
                            'periods': periods, 'targets': public_masks, 'assessments': reports,
                            'validationExcludedFromSearch': True, 'decision': 'candidate-only',
                            'converged': converged, 'boundaryParameters': touching, 'termination': termination,
                            'bestObjective': stage['bestObjective'], 'baselineMetrics': baseline_metrics}
    final['calibrationWorkflow'] = {'target': 'integrated', 'stages': [stage]}
    write_json(folder / 'result.json', final)
    return final
