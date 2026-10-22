from __future__ import annotations

import math

import numpy as np
import pandas as pd
from .calibration_config import RUNOFF_KEYS, ROUTING_KEYS, PROFILES, flow_objective

from .common import write_json
from .model_core import PARAMETER_DEFINITIONS, parameters
from .simulation import prepare, run_simulation


def calibrate(db, request, folder, progress=None, cancelled=None):
    stage = request.get('stage', 'runoff')
    if stage not in ('runoff', 'routing', 'lake') or int(request.get('calibrationYear', 2024)) != 2024:
        raise ValueError('自动搜索仅使用2024年率定期，验证和延伸检验不能参与搜索')
    if request.get('_parentState'):
        raise ValueError('自动率定各候选须从统一起点预热，不能继承固定参数形成的父状态')
    base = parameters(request.get('parameters'))
    if stage == 'routing' and request.get('routingMode', 'source-separated') != 'source-separated':
        raise ValueError('分水源汇流率定须采用分水源调蓄结构')
    keys = {'runoff': RUNOFF_KEYS, 'routing': ROUTING_KEYS, 'lake': ['lakeCoefficient']}[stage]
    flow_stage = stage != 'lake'
    definitions = {d['key']: d for d in PARAMETER_DEFINITIONS}
    bounds = np.array([[definitions[k]['min'], definitions[k]['max']] for k in keys])
    maximum = min(2000, max(10, int(request.get('maxEvaluations', 120 if flow_stage else 30))))
    seed = int(request.get('seed', 42))
    rng = np.random.default_rng(seed)
    source = prepare(db, request['inputId'])
    if source['times'][0].year > 2023 or str(request.get('start', '2023'))[:4] > '2023':
        raise ValueError('正式率定须包含2023年统一预热，不允许从率定期中途起算')
    start = pd.Timestamp(request.get('start') or source['times'][0])
    end = pd.Timestamp(request.get('end') or source['times'][-1])
    if start > pd.Timestamp('2023-01-01') or end < pd.Timestamp('2025-01-01') or source['times'][0] > pd.Timestamp('2023-01-01') or source['times'][-1] < pd.Timestamp('2025-01-01'):
        raise ValueError('正式率定必须覆盖完整2023预热与2024评价，结束时刻至少为2025-01-01 00:00')
    profile = request.get('objectiveProfile', 'balanced')
    if profile not in PROFILES:
        raise ValueError('未知率定目标')
    trace, best, best_params = [], math.inf, base.copy()
    baseline_metrics = None

    def evaluate(vector):
        nonlocal best, best_params, baseline_metrics
        if cancelled and cancelled():
            raise InterruptedError('用户停止率定')
        candidate = {**base, **dict(zip(keys, map(float, vector)))}
        result = run_simulation(db, {**request, 'parameters': candidate, '_skipDiagnostics': True}, prepared=source, cancelled=cancelled)
        metric = result['metrics'].get('2024', {}).get('flow' if flow_stage else 'lake', {})
        if metric.get('count', 0) < 30:
            raise ValueError('2024年可靠观测匹配不足30日，不能自动率定')
        if flow_stage:
            if metric.get('nse') is None:
                raise ValueError('率定期实测流量无有效方差')
            objective, components = flow_objective(metric, profile)
        else:
            objective = metric['rmse']
            components = {'lakeRmse': objective}
        if not trace:
            baseline_metrics = metric
        if objective < best:
            best, best_params = objective, candidate.copy()
        trace.append({'evaluation': len(trace) + 1, 'objective': float(objective), 'bestObjective': float(best),
                      'parameters': {k: candidate[k] for k in keys}, 'metrics': metric, 'components': components})
        write_json(folder / 'search-progress.json', {'stage': stage, 'evaluations': trace, 'bestParameters': best_params})
        if progress:
            label = {'runoff': '陆面产流', 'routing': '分水源汇流', 'lake': '湖泊'}[stage]
            progress(len(trace) / maximum * 90, f'{label}率定 {len(trace)}/{maximum}，目标值 {best:.5g}')
        return float(objective)

    baseline_vector = np.array([base[k] for k in keys])
    evaluate(baseline_vector)
    population_size = min(maximum - 1, max(6, len(keys) * 2))
    population = bounds[:, 0] + rng.random((population_size, len(keys))) * (bounds[:, 1] - bounds[:, 0])
    population[0] = baseline_vector
    def feasible(vector):
        # Keep the declared box bounds while enforcing the physical ordering.
        # Reject impossible points with a penalty; never clip one searched key
        # into another, which would alter the meaning of the recorded candidate.
        candidate = {**base, **dict(zip(keys, map(float, vector)))}
        return candidate['baseflowTauHours'] >= candidate['interflowTauHours']
    def score_candidate(vector):
        return evaluate(vector) if feasible(vector) else math.inf
    # The intersection is large for these bounds; resample invalid initial points.
    for i in range(1, len(population)):
        while not feasible(population[i]):
            population[i] = bounds[:, 0] + rng.random(len(keys)) * (bounds[:, 1] - bounds[:, 0])
    scores = np.array([evaluate(v) for v in population if len(trace) < maximum])
    population = population[:len(scores)]
    converged, proposals, invalid_proposals = False, 0, 0
    while len(trace) < maximum and proposals < maximum * 20:
        for i in range(len(population)):
            choices = [j for j in range(len(population)) if j != i]
            a, b, c = rng.choice(choices, 3, replace=False)
            mutant = np.clip(population[a] + .7 * (population[b] - population[c]), bounds[:, 0], bounds[:, 1])
            cross = rng.random(len(keys)) < .8
            cross[rng.integers(len(keys))] = True
            trial = np.where(cross, mutant, population[i])
            proposals += 1
            invalid_proposals += int(not feasible(trial))
            score = score_candidate(trial)
            if score < scores[i]:
                population[i], scores[i] = trial, score
            if len(trace) >= maximum:
                break
        if len(trace) >= population_size * 3 and np.std(scores) < 1e-6:
            converged = True
            break
    final = run_simulation(db, {**request, 'parameters': best_params}, folder=folder, cancelled=cancelled)
    final['calibration'] = {'stage': stage, 'algorithm': 'differential-evolution', 'seed': seed,
                            'searchParameters': keys, 'objectiveProfile': profile if flow_stage else 'lake-rmse',
                            'objectiveWeights': PROFILES[profile]['weights'] if flow_stage else {'lakeRmse': 1.}, 'calibrationYear': 2024, 'evaluations': len(trace),
                            'maxEvaluations': maximum, 'converged': converged,
                            'termination': 'converged' if converged else ('budget-exhausted' if len(trace) >= maximum else 'proposal-limit'),
                            'invalidProposalsSkipped': invalid_proposals,
                            'baselineMetrics': baseline_metrics, 'bestObjective': best, 'trace': trace,
                            'frozenParameters': [k for k in base if k not in keys],
                            'validationYearsExcludedFromSearch': [2025, 2026],
                            'boundaryParameters': [k for k in keys if min(abs(best_params[k]-definitions[k]['min']),abs(best_params[k]-definitions[k]['max'])) < 1e-8]}
    write_json(folder / 'result.json', final)
    return final
