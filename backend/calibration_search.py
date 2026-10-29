"""Budgeted, resumable three-start differential evolution for shared hydrology."""
from copy import deepcopy
import json
import math

import numpy as np

from .calibration_config import LAND_KEYS, SNOW_KEYS, ROUTING_KEYS, FLOW_SELECTABLE_KEYS
from .common import digest, now, read_json, write_json, sha_file
from .model_core import parameters

DEFAULT_BUDGETS = {'runoff':6000,'snow':6000,'routing':3000,'joint':3000}
DEFAULT_SEEDS = [42,142,242]


def schedule(budgets, selected):
    rows = []
    for cycle in range(2):
        for name, group in (('runoff',LAND_KEYS),('snow',SNOW_KEYS),('routing',ROUTING_KEYS)):
            keys = [k for k in selected if k in group]
            budget = budgets[name]//2+(budgets[name]%2 if cycle==1 else 0)
            if keys and budget:
                rows.append({'name':name,'cycle':cycle+1,'keys':keys,'budget':budget})
    if budgets['joint']:
        rows.append({'name':'joint','cycle':1,'keys':list(selected),'budget':budgets['joint']})
    return rows


def run_fit(evaluator, request, folder, label, train_start, train_end,
            validation_start, validation_end, initial, progress, cancelled,
            candidates=()):
    folder.mkdir(parents=True,exist_ok=True)
    options = request['twoStage']
    selected = request['search']['keys']
    if not selected or set(selected)-set(FLOW_SELECTABLE_KEYS):
        raise ValueError('请选择影响流量的水文参数')
    bounds = {k:request['search']['bounds'][k] for k in selected}
    seeds = options.get('seeds',DEFAULT_SEEDS)
    budgets = options.get('budgets',DEFAULT_BUDGETS)
    mask = evaluator.mask(train_start,train_end)
    evidence = evaluator.evidence(mask)
    if evidence['trainingPairs']<30:
        raise ValueError('所选流量率定期不足30个有效日')
    identity = digest({'configuration':request['schemeSnapshot']['configurationSHA256'],
        'model':request['_modelIdentity'],'inputId':request['inputId'],'label':label,
        'train':[train_start,train_end],'validation':[validation_start,validation_end],
        'initial':initial,'bounds':bounds,'seeds':seeds,'budgets':budgets,
        'observations':evidence['fixedTrainingObservationSHA256'],
        'searchImplementation':sha_file(__file__)})
    completed = read_json(folder/'fit-result.json')
    if completed:
        if completed['identity']!=identity:
            raise ValueError('接续率定的配置已改变')
        return completed
    state = read_json(folder/'optimizer-state.json')
    if state and state['identity']!=identity:
        raise ValueError('搜索状态与本次条件不一致')
    if not state:
        state = {'identity':identity,'working':parameters(initial),'evaluations':0,'records':[],
                 'schedule':schedule(budgets,selected),'stageIndex':0,'active':None,
                 'extended':False,'initialCandidates':0,'createdAt':now(),
                 'bestHistory':[],'baselineMetrics':None}
    trace_path = folder/'search-trace.jsonl'
    trace = trace_path.open('a',encoding='utf-8',newline='\n')
    evaluations_at_open = state['evaluations']
    best = state.get('objective')
    if best is None:
        best = math.inf
    deferred_progress_error = []

    def checkpoint():
        trace.flush()
        state['updatedAt'] = now()
        write_json(folder/'optimizer-state.json',state)
        current = state['active']
        write_json(folder/'search-progress.json',{'fit':label,'evaluations':state['evaluations'],
            'maxEvaluations':sum(s['budget'] for s in state['schedule']),
            'bestObjective':state.get('objective'),'bestParameters':state['working'],
            'currentStage':current['name'] if current else None,'stages':state['records'],
            'traceFile':'search-trace.jsonl','traceCount':state['evaluations'],
            'trainingPeriod':{'start':train_start,'end':train_end},
            'validationPeriod':{'start':validation_start,'end':validation_end},
            'validationExcludedFromSearch':True,**evidence})
        if deferred_progress_error:
            raise deferred_progress_error.pop()

    def evaluate(candidate, phase, seed=None):
        nonlocal best
        if cancelled():
            raise InterruptedError('已停止率定，搜索状态已保存，可继续')
        candidate = parameters(candidate)
        objective,metric = evaluator.objective(candidate,mask)
        if not np.isfinite(objective):
            raise ValueError('率定目标无效')
        state['evaluations'] += 1
        if state['baselineMetrics'] is None:
            state['baselineMetrics'] = metric
        if objective<best:
            best = float(objective)
            state['objective'],state['working'] = best,deepcopy(candidate)
        state['bestHistory'].append(best)
        active = state['active']
        item = {'fit':label,'evaluation':state['evaluations'],'stage':active['name'] if active else 'initial',
                'cycle':active.get('cycle') if active else None,'phase':phase,'seed':seed,
                'objective':float(objective),'bestObjective':best,
                'parameters':{k:candidate[k] for k in selected},'metrics':metric}
        trace.write(json.dumps(item,ensure_ascii=False,allow_nan=False,separators=(',',':'))+'\n')
        total = sum(s['budget'] for s in state['schedule'])
        try:
            progress(min(99.,100*state['evaluations']/max(1,total)),
                     '%s · %s · %d次 · 最优 %.5g'%(label,item['stage'],state['evaluations'],best))
        except Exception as error:
            # Commit the evaluated candidate, population and RNG together
            # before a stop or a progress-report failure unwinds the search.
            deferred_progress_error.append(error)
        return float(objective)

    try:
        # Initial and transferred fit vectors are counted in the first block.
        initial_candidates = [initial,*candidates]
        while state['initialCandidates']<len(initial_candidates):
            evaluate(initial_candidates[state['initialCandidates']],'initial')
            state['initialCandidates'] += 1
            checkpoint()
        while state['stageIndex']<len(state['schedule']):
            block = state['schedule'][state['stageIndex']]
            if state['active'] is None:
                base = deepcopy(state['working'])
                state['active'] = {**block,'base':base,'startEvaluations':state['evaluations'],
                    'used':state['initialCandidates'] if state['stageIndex']==0 else 0,
                    'responseIndex':0,'responses':{},'seedIndex':0,'starts':[],'de':None}
            active = state['active']
            keys = active['keys']
            # A counted local response is reported, but a single response does
            # not silently remove a user-selected parameter from the search.
            while active['responseIndex']<len(keys) and active['used']<active['budget']:
                key = keys[active['responseIndex']]
                lo,hi = bounds[key]
                base = active['base']
                trial = min(hi,base[key]+.05*(hi-lo))
                if trial==base[key]:
                    trial=max(lo,base[key]-.05*(hi-lo))
                try:
                    candidate = parameters({**base,key:trial})
                except ValueError:
                    candidate = None
                if candidate:
                    value = evaluate(candidate,'response')
                    active['used'] += 1
                    active['responses'][key] = {'perturbation':trial,'objective':value,
                                                'retainedInSearch':True}
                else:
                    active['responses'][key] = {'constraintBlocked':True,'retainedInSearch':True}
                active['responseIndex'] += 1
                checkpoint()
            while active['seedIndex']<len(seeds) and active['used']<active['budget']:
                seed = seeds[active['seedIndex']]
                if active['de'] is None:
                    remaining = active['budget']-active['used']
                    quota = remaining//(len(seeds)-active['seedIndex'])
                    # Keep later starts useful for very small user budgets.
                    if quota==0:
                        active['seedIndex'] += 1
                        continue
                    base = deepcopy(state['working'])
                    rng = np.random.default_rng(seed)
                    base_score = float(state['objective'])
                    active['de'] = {'quota':quota,'used':0,'base':base,'rng':rng.bit_generator.state,
                        'population':[[base[k] for k in keys]],'scores':[base_score],
                        'proposal':0,'slot':0,'generation':0,'generationBest':[],
                        'converged':False,'termination':'budget-exhausted'}
                de = active['de']
                rng = np.random.default_rng()
                rng.bit_generator.state = de['rng']
                box = np.asarray([bounds[k] for k in keys],float)
                size = max(12,4*len(keys))
                while de['used']<de['quota']:
                    if cancelled():
                        raise InterruptedError('已停止率定，搜索状态已保存，可继续')
                    de['proposal'] += 1
                    population = np.asarray(de['population'])
                    slot = None
                    if len(population)<size:
                        vector = box[:,0]+rng.random(len(keys))*(box[:,1]-box[:,0])
                    else:
                        slot = de['slot']
                        choices = [i for i in range(len(population)) if i!=slot]
                        a,b,c = rng.choice(choices,3,replace=False)
                        # Mixed scales explore both the existing basin solution
                        # and the whole allowed physical box.
                        scale = rng.uniform(.5,.95)
                        mutant = np.clip(population[a]+scale*(population[b]-population[c]),box[:,0],box[:,1])
                        cross = rng.random(len(keys))<.85
                        cross[rng.integers(len(keys))] = True
                        vector = np.where(cross,mutant,population[slot])
                    candidate = {**de['base'],**dict(zip(keys,map(float,vector)))}
                    try:
                        parameters(candidate)
                    except ValueError:
                        if de['proposal']>max(100,de['quota']*100):
                            de['termination']='constraint-proposal-limit'
                            break
                        continue
                    score = evaluate(candidate,'search',seed)
                    de['used'] += 1
                    active['used'] += 1
                    if slot is None:
                        de['population'].append(vector.tolist())
                        de['scores'].append(score)
                    else:
                        if score<de['scores'][slot]:
                            de['population'][slot],de['scores'][slot] = vector.tolist(),score
                        de['slot'] = (slot+1)%size
                        if de['slot']==0:
                            de['generation'] += 1
                            history = de['generationBest']
                            history.append(min(de['scores']))
                            spread = float(np.std(de['scores']))
                            stagnant = (len(history)>=20 and history[-20]-history[-1]
                                        <=1e-5*max(1.,abs(history[-20])))
                            if (spread<=1e-6*max(1.,abs(np.mean(de['scores']))) or
                                    stagnant and spread<=1e-3*max(1.,abs(history[-1]))):
                                de['converged']=True
                                de['termination']='converged'
                    de['rng']=rng.bit_generator.state
                    if state['evaluations']%25==0 or de['converged'] or deferred_progress_error:
                        checkpoint()
                    if de['converged']:
                        break
                population_file='population-%02d-%d.json'%(state['stageIndex']+1,seed)
                write_json(folder/population_file,{'stage':active['name'],'cycle':active['cycle'],
                    'seed':seed,'keys':keys,'baseParameters':de['base'],**deepcopy(de)})
                active['starts'].append({'seed':seed,'evaluations':de['used'],'budget':de['quota'],
                    'converged':de['converged'],'termination':de['termination'],
                    'generations':de['generation'],'bestObjective':min(de['scores']),
                    'populationFile':population_file,'populationSHA256':sha_file(folder/population_file)})
                active['de']=None
                active['seedIndex'] += 1
                checkpoint()
            record = {k:deepcopy(active[k]) for k in ('name','cycle','keys','budget','used','responses','starts')}
            record.update(bestObjective=state['objective'],bestParameters={k:state['working'][k] for k in keys},
                          converged=bool(active['starts']) and all(s['converged'] for s in active['starts']))
            state['records'].append(record)
            state['active']=None
            state['stageIndex'] += 1
            if state['stageIndex']==len(state['schedule']) and not state['extended'] and options.get('allowExtension',True):
                history=state['bestHistory']
                earlier=history[max(0,len(history)-max(100,len(history)//5))]
                improvement=(earlier-history[-1])/max(abs(earlier),1e-12)
                if improvement>1e-4 and not record['converged'] and sum(budgets.values())<36000:
                    available=min(sum(budgets.values()),36000-sum(budgets.values()))
                    extra={k:max(1,int(v*available/sum(budgets.values()))) for k,v in budgets.items()}
                    state['schedule'].extend(schedule(extra,selected))
                    state['extended']=True
            checkpoint()
        candidate=state['working']
        near=[k for k in selected if min(candidate[k]-bounds[k][0],bounds[k][1]-candidate[k])
              <=.01*(bounds[k][1]-bounds[k][0])]
        report={'identity':identity,'fit':label,'algorithm':'alternating-multistart-de-with-joint-refinement-v1',
            'parameters':candidate,'initialParameters':initial,'bounds':bounds,'seeds':seeds,
            'evaluations':state['evaluations'],'extended':state['extended'],'stages':state['records'],
            'baselineMetrics':state['baselineMetrics'],'bestObjective':state['objective'],
            'converged':state['records'][-1]['converged'],'nearBoundaryParameters':near,
            'termination':'converged' if state['records'][-1]['converged'] else 'budget-exhausted',
            'trainingPeriod':{'start':train_start,'end':train_end},
            'validationPeriod':{'start':validation_start,'end':validation_end},
            'training':evaluator.assess(candidate,train_start,train_end),
            'validation':evaluator.assess(candidate,validation_start,validation_end) if validation_start else None,
            'validationExcludedFromSearch':True,'objectiveProfile':evaluator.profile,
            'traceFile':'search-trace.jsonl','traceSHA256':None,**evidence}
        trace.flush()
        report['traceSHA256']=sha_file(trace_path)
        write_json(folder/'fit-result.json',report)
        return report
    finally:
        checkpoint()
        trace.close()
