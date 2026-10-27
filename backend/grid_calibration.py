"""Research calibration consumes the saved configuration exactly, with held-out data absent."""
from copy import deepcopy
import math
import numpy as np
import pandas as pd
from .common import write_json
from .model_core import parameters
from .calibration_config import ROUTING_KEYS,flow_objective
from .simulation import metrics
from .grid_simulation import run as simulate,prepare

def run(db,request,folder,progress,cancelled):
    search=request['search'];periods=request['periods'];target=request['target'];scope=search['scope'];selected=search['keys']
    stages=[('lake',selected)] if target=='lake' else [(stage,[k for k in selected if (k in ROUTING_KEYS)==(stage=='routing')]) for stage in ('runoff','routing')]
    stages=[(stage,keys) for stage,keys in stages if keys]
    if not stages:raise ValueError('没有参与率定的参数')
    source=prepare(db,request['inputId'],request['spatialVersion']);working=deepcopy(request);stage_records=[];all_trace=[]
    observation=deepcopy(request['_observations']);train_start=pd.Timestamp(periods['trainingStart']);train_end=pd.Timestamp(periods['trainingEnd'])
    # Only pre-training initialization and training observations are present in search runs.
    for key in ('level','flow'):observation[key]={t:v for t,v in observation[key].items() if pd.Timestamp(t)<train_end}
    rng=np.random.default_rng(search['seed'])
    for stage_index,(stage,keys) in enumerate(stages):
        if cancelled():raise InterruptedError('用户停止研究率定，搜索轨迹已保留')
        baseline={**working['parameters'],**(working.get('partitionParameters',{}).get('duja',{}) if scope=='duja' and target!='lake' else {})}
        bounds=np.array([search['bounds'][k] for k in keys],dtype=float);budget=search['budgets'][stage]
        best=math.inf;best_parameters=baseline.copy();trace=[];baseline_metric=None;proposals=0;converged=False
        def conditions(candidate,template):
            result=deepcopy(template)
            if scope=='duja' and target!='lake':result['partitionParameters']['duja']={k:v for k,v in candidate.items() if k!='lakeCoefficient'}
            else:result['parameters']=candidate
            return result
        def evaluate(vector):
            nonlocal best,best_parameters,baseline_metric
            if cancelled():raise InterruptedError('用户停止研究率定，搜索轨迹已保留')
            candidate=parameters({**baseline,**dict(zip(keys,map(float,vector)))})
            trial=conditions(candidate,{**working,'end':periods['trainingEnd'],'_observations':observation,'_metricsOnly':True})
            trial.pop('evaluationStart',None);trial.pop('evaluationEnd',None)
            result=simulate(db,trial,prepared=source,cancelled=cancelled)
            frame=pd.DataFrame(result['daily']);frame.index=pd.to_datetime(frame.date)
            frame=frame.loc[(frame.index>=train_start)&(frame.index<train_end)]
            if target=='lake':
                initial=result.get('initialization')
                if initial:frame=frame.loc[frame.index!=pd.Timestamp(initial['time']).normalize()]
                metric=metrics(frame.observedLevel,frame.lakeLevel);objective=metric['rmse']
            else:metric=metrics(frame.observedFlow,frame.duja);objective,_=flow_objective(metric,search['objectiveProfile'])
            if metric['count']<30 or objective is None or not np.isfinite(objective):raise ValueError('训练期可靠配对不足30日或目标无效')
            if baseline_metric is None:baseline_metric=metric
            if objective<best:best=float(objective);best_parameters=candidate.copy()
            item={'stage':stage,'evaluation':len(trace)+1,'objective':float(objective),'bestObjective':best,'parameters':{k:candidate[k] for k in keys},'metrics':metric}
            trace.append(item);all_trace.append(item)
            write_json(folder/'search-progress.json',{'stages':stage_records,'currentStage':stage,'trace':all_trace,'bestParameters':best_parameters,'trainingPeriod':{'start':periods['trainingStart'],'end':periods['trainingEnd']},'evaluationExcluded':True,'configurationSHA256':request['schemeSnapshot']['configurationSHA256']})
            progress((stage_index+len(trace)/budget)/len(stages)*90,f'{stage} 搜索 {len(trace)}/{budget} · 最佳 {best:.5g}')
            return float(objective)
        def feasible(vector):
            try:parameters({**baseline,**dict(zip(keys,map(float,vector)))});return True
            except ValueError:return False
        initial=np.array([baseline[k] for k in keys]);evaluate(initial)
        size=min(max(0,budget-1),max(6,len(keys)*2));population=[];scores=[]
        while len(population)<size and len(trace)<budget and proposals<budget*50:
            vector=bounds[:,0]+rng.random(len(keys))*(bounds[:,1]-bounds[:,0]);proposals+=1
            if not feasible(vector):continue
            population.append(vector);scores.append(evaluate(vector))
        while len(trace)<budget and proposals<budget*50:
            for i in range(len(population)):
                proposals+=1
                if len(population)>=4:
                    choices=[j for j in range(len(population)) if j!=i];a,b,c=rng.choice(choices,3,replace=False)
                    mutant=np.clip(population[a]+.7*(population[b]-population[c]),bounds[:,0],bounds[:,1]);cross=rng.random(len(keys))<.8;cross[rng.integers(len(keys))]=True;candidate=np.where(cross,mutant,population[i])
                else:candidate=bounds[:,0]+rng.random(len(keys))*(bounds[:,1]-bounds[:,0])
                if not feasible(candidate):continue
                score=evaluate(candidate)
                if score<scores[i]:scores[i]=score;population[i]=candidate
                if len(trace)>=budget:break
            if not population:break
            if len(trace)>=max(12,len(population)*3) and np.std(scores)<1e-6:converged=True;break
        touching=[k for k in keys if min(abs(best_parameters[k]-search['bounds'][k][0]),abs(best_parameters[k]-search['bounds'][k][1]))<1e-8]
        record={'stage':stage,'evaluations':len(trace),'maxEvaluations':budget,'keys':keys,'bounds':{k:search['bounds'][k] for k in keys},'initialParameters':{k:baseline[k] for k in keys},'bestParameters':{k:best_parameters[k] for k in keys},'baselineMetrics':baseline_metric,'bestObjective':best,'converged':converged,'termination':'converged' if converged else 'budget-exhausted' if len(trace)>=budget else 'constraint-proposal-limit','boundaryParameters':touching}
        stage_records.append(record);working=conditions(best_parameters,working)
    if cancelled():raise InterruptedError('用户停止研究率定，搜索轨迹已保留')
    final=simulate(db,{**working,'_metricsOnly':False},folder,lambda p,m:progress(90+p*.09,m),cancelled,source)
    final['calibration']={'algorithm':'differential-evolution','seed':search['seed'],'evaluations':len(all_trace),'trace':all_trace,'stages':stage_records,'objectiveProfile':search['objectiveProfile'] if target!='lake' else 'continuous-main-lake-rmse','periods':periods,'searchObservationYears':sorted({int(t[:4]) for t in observation['flow' if target!='lake' else 'level'] if train_start<=pd.Timestamp(t)<train_end}),'validationExcludedFromSearch':True,'decision':'candidate-only','converged':all(s['converged'] for s in stage_records),'boundaryParameters':sorted({k for s in stage_records for k in s['boundaryParameters']}),'termination':'all-stages-completed','bestObjective':stage_records[-1]['bestObjective'],'baselineMetrics':stage_records[0]['baselineMetrics']}
    final['calibrationWorkflow']={'target':target,'stages':stage_records};write_json(folder/'result.json',final)
    return final
