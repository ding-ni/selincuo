"""Fresh grouped hydrology searches followed by continuous Q/H refinement."""
from copy import deepcopy

import numpy as np
import pandas as pd

from .calibration_objective import combined_score, validate_objective
from .calibration_search import run_fit
from .common import digest, read_json, sha_file, write_json
from .fast_calibration import DujaEvaluator
from .fast_basin_calibration import BasinEvaluator
from .lake_calibration import freeze_window, joint_fit, joint_assess, assess
from .lake_constrained_calibration import (
    _rows, _candidate, _fast_parity, _evaluate_candidate, _refine,
    _read_series, _yearly_flow, _water_budget, choose_candidates,
)


def generated_candidates(path, selected, bounds):
    """Only the current fit's evaluated vectors enter the basin candidate pool."""
    found = {}
    for row in _rows(path):
        values = row['parameters']
        identifier = digest({key: values[key] for key in selected})
        found.setdefault(identifier, {
            'candidateId': identifier, 'parameters': values,
            'flowObjective': row['objective'], 'flow': row['metrics'],
            'sourceEvaluation': row['evaluation'],
        })
    return list(found.values())


def _linked_series(descriptor, phase):
    return {**descriptor, 'file': phase + '/joint/' + descriptor['file']}


def select_candidates(records, selected, bounds, initial, limit):
    chosen=choose_candidates(records,selected,bounds,limit)
    initial_id=digest({key:initial[key] for key in selected})
    incumbent=next((row for row in records if row['candidateId']==initial_id),None)
    if not incumbent:
        raise ValueError('本次搜索轨迹缺少真实初值评价')
    if chosen and all(row['candidateId']!=initial_id for row in chosen):
        chosen[-1]=incumbent
    return chosen


def run_selection(db, request, source, folder, progress, cancelled):
    folder.mkdir(parents=True, exist_ok=True)
    options, periods = request['twoStage'], request['periods']
    objective = validate_objective(options['objective'])
    anchor = request.get('_fixedInitialization')
    if not anchor:
        raise ValueError('完整率定需要湖位起算值')
    lake_periods, observation = options['lakePeriods'], request['_observations']['level']
    def window(start, end):
        return {**freeze_window(observation, start, end, anchor), 'objective': objective}
    train_window = window(lake_periods['trainingStart'], lake_periods['trainingEnd'])
    test_window = window(lake_periods['evaluationStart'], lake_periods['evaluationEnd'])
    all_window = window(lake_periods['trainingStart'], lake_periods['evaluationEnd'])
    identity = digest({
        'algorithm': 'fresh-grouped-hydrology-continuous-dual-v1',
        'inputId': request['inputId'], 'inputManifest': source['manifest'],
        'spatialVersion': request['spatialVersion'], 'model': request['_modelIdentity'],
        'request': {key: request.get(key) for key in ('start', 'end', 'parameters',
            'initialHydrology', 'search', 'periods', 'twoStage', '_observations', '_fixedInitialization')},
        'implementationSHA256': sha_file(__file__),
    })
    previous = read_json(folder / 'identity.json')
    if previous and previous['identity'] != identity:
        raise ValueError('率定资料或设置已改变，请保存新修订后运行')
    if not previous:
        write_json(folder / 'identity.json', {'identity': identity, 'objective': objective,
            'trainingLake': train_window, 'testingLake': test_window, 'allLake': all_window})
    completed = read_json(folder / 'selection-result.json')
    if completed:
        if completed['identity'] != identity:
            raise ValueError('率定结果与配置不一致')
        return completed
    evaluator = DujaEvaluator(source, request, end=max(periods['trainingEnd'], periods['evaluationEnd']))
    basin = BasinEvaluator(source, request)
    phases = [('training', '分期率定与检验', periods['trainingStart'], periods['trainingEnd'],
               periods['evaluationStart'], periods['evaluationEnd'], train_window)]
    if options['finalRefit']:
        phases.append(('all-data', '全部资料正式定参', min(periods['trainingStart'], periods['evaluationStart']),
                       max(periods['trainingEnd'], periods['evaluationEnd']), None, None, all_window))
    records, flow_fits, generation_fits = [], [], []
    for phase_index, (phase, label, start, end, check_start, check_end, lake_window) in enumerate(phases):
        phase_root = folder / phase
        phase_root.mkdir(parents=True, exist_ok=True)
        offset = phase_index / len(phases) * 81.
        width = 81. / len(phases)
        root_state = read_json(folder.parent / 'pipeline-state.json', {})
        def stage(name):
            root_state.update(phase='continuous-dual', fit='joint/' + phase + '/' + name,
                              phaseLabel=label, fitIndex=phase_index, fitCount=len(phases))
            write_json(folder.parent / 'pipeline-state.json', root_state)
        stage('flow')
        fit = run_fit(evaluator, request, phase_root / 'flow', label + ' · 水文参数',
                      start, end, check_start, check_end, deepcopy(request['parameters']),
                      lambda p, m: progress(offset + width * .48 * p / 100., m), cancelled)
        generation_fits.append(fit)
        unique = generated_candidates(phase_root / 'flow/search-trace.jsonl',
                                      request['search']['keys'], request['search']['bounds'])
        chosen = select_candidates(unique, request['search']['keys'],
                                   request['search']['bounds'], request['parameters'], options['candidateLimit'])
        if not chosen:
            raise ValueError('本次水文搜索没有有效候选')
        phase_folder = phase_root / 'joint'
        phase_folder.mkdir(parents=True, exist_ok=True)
        write_json(phase_folder / 'candidate-pool.json', {'identity': identity,
            'phase': phase, 'source': 'current-fit-generated-candidates',
            'generationEvaluations': fit['evaluations'], 'uniqueGenerated': len(unique),
            'initialCandidateRetained': True,
            'initialCandidateId': digest({k:request['parameters'][k] for k in request['search']['keys']}),
            'flowThresholdsApplied': False, 'candidateIds': [r['candidateId'] for r in chosen]})
        stage('joint')
        vectors = [_candidate(request['parameters'], r['parameters'], request['search']['keys'],
                              request['search']['bounds']) for r in (chosen[0], chosen[-1])]
        parity = _fast_parity(db, request, source, basin, vectors, phase_folder, cancelled)
        cached = {r['candidateId']: r for r in _rows(phase_folder / 'lake-candidates.jsonl')}
        score = lambda row: combined_score(row['flowObjective'], row['trainingLake']['jointScore'], objective)
        for i, row in enumerate(chosen):
            if cancelled():
                raise InterruptedError('已停止率定，已完成候选可继续')
            _evaluate_candidate(row, request, basin, lake_window, phase_folder, cached, cancelled)
            if (i + 1) % 5 == 0 or i + 1 == len(chosen):
                best = min(score(r) for r in cached.values())
                write_json(phase_folder / 'search-progress.json', {'phase': 'basin-candidates',
                    'evaluations': i + 1, 'maxEvaluations': len(chosen), 'bestObjective': best,
                    'candidateCount': len(cached), 'message': label + ' · 全流域候选'})
                progress(offset + width * (.48 + .15 * (i + 1) / len(chosen)),
                         label + ' · 全流域候选 %d/%d' % (i + 1, len(chosen)))
        refinement = _refine(evaluator, basin, request, evaluator.mask(start, end), lake_window,
                             phase_folder, list(cached.values()), cached,
                             lambda p, m: progress(offset + width * (.63 + .35 * (p - 62.) / 18.),
                                                   label + ' · ' + m), cancelled)
        best = min(cached.values(), key=lambda r: (score(r), r['candidateId']))
        frame = _read_series(phase_folder, best['series'], observation)
        lake_fit = joint_fit(frame, request['parameters']['lakeCoefficient'],
                            request['search']['bounds']['lakeCoefficient'], lake_window, anchor)
        selected = {**request['parameters'], **best['parameters'], 'lakeCoefficient': lake_fit['value']}
        if check_start:
            lake_fit['validation'] = {**assess(frame, lake_fit['value'], request['parameters']['lakeCoefficient'],
                test_window['start'], test_window['end'], anchor),
                **joint_assess(frame, lake_fit['value'], request['parameters']['lakeCoefficient'], test_window)}
            lake_fit['validationPeriod'] = {'start': test_window['start'], 'end': test_window['end']}
        flow_fit = {**deepcopy(fit), 'fit': label, 'algorithm': 'fresh-grouped-then-continuous-dual-v1',
            'parameters': selected, 'training': evaluator.assess(selected, start, end),
            'validation': evaluator.assess(selected, check_start, check_end) if check_start else None,
            'bestObjective': score(best), 'flowObjective': best['flowObjective'],
            'lakeObjective': lake_fit['jointScore'],
            'evaluations': fit['evaluations'] + refinement['evaluations'],
            'hydrologyEvaluations': fit['evaluations'], 'jointEvaluations': refinement['evaluations'],
            'converged': fit['converged'] and refinement['converged'],
            'termination': refinement['termination'], 'validationExcludedFromSearch': bool(check_start)}
        flow_fits.append(flow_fit)
        record = {'phase': phase, 'label': label, 'candidateId': best['candidateId'],
            'parameters': selected, 'series': _linked_series(best['series'], phase), 'fit': lake_fit,
            'jointObjective': score(best), 'flowObjective': best['flowObjective'],
            'generated': fit['evaluations'], 'uniqueGenerated': len(unique),
            'selectedForBasin': len(chosen), 'basinCandidatesEvaluated': len(cached),
            'refinement': refinement, 'fastParity': parity}
        records.append(record)
        write_json(phase_root / 'phase-result.json', record)
    training, final = records[0], records[-1]
    final_frame = _read_series(folder, final['series'], observation)
    flow = _yearly_flow(evaluator, final['parameters'], periods)
    water = _water_budget(final_frame, final['fit']['value'], request['parameters']['lakeCoefficient'],
                          anchor, basin.lake_area)
    write_json(folder / 'water-budget.json', water)
    selection = {'identity': identity, 'selectionMode': 'continuous-dual',
        'algorithm': 'fresh-grouped-hydrology-continuous-dual-v1', 'objective': objective,
        'parameters': final['parameters'], 'initialParameters': deepcopy(request['parameters']),
        'selectedParameters': list(request['search']['keys']), 'bounds': deepcopy(request['search']['bounds']),
        'seeds': list(options['seeds']), 'flowThresholdsApplied': False, 'targetBasedStopping': False,
        'flowFits': flow_fits, 'generationFits': generation_fits, 'phases': records,
        'totalGenerated': sum(r['generated'] for r in records),
        'uniqueGenerated': sum(r['uniqueGenerated'] for r in records),
        'totalRescored': 0, 'selectedForBasin': sum(r['selectedForBasin'] for r in records),
        'basinCandidatesEvaluated': sum(r['basinCandidatesEvaluated'] for r in records),
        'refinement': {'evaluations': sum(r['refinement']['evaluations'] for r in records),
            'converged': all(r['refinement']['converged'] for r in records),
            'termination': final['refinement']['termination']},
        'trainingCandidateId': training['candidateId'], 'finalCandidateId': final['candidateId'],
        'trainingParameters': training['parameters'], 'trainingSeries': training['series'],
        'finalSeries': final['series'], 'trainingFit': training['fit'], 'finalFit': final['fit'],
        'flowMetrics': flow, 'flowObjective': final['flowObjective'], 'jointObjective': final['jointObjective'],
        'allDataEvaluation': joint_assess(final_frame, final['fit']['value'],
                                         request['parameters']['lakeCoefficient'], all_window),
        'trainingUsesValidationLake': False, 'trainingUsesValidationFlow': False,
        'finalUsesAllLakeObservations': bool(options['finalRefit']),
        'flowParameterSelectionUsesLakeLevel': True, 'fastParity': [r['fastParity'] for r in records],
        'waterBudgetFile': 'water-budget.json', 'dataUse': deepcopy(request.get('inputProvenance', {}))}
    write_json(folder / 'selection-result.json', selection)
    return selection
