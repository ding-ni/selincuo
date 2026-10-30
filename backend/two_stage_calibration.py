"""Flow split checks, all-data hydrology, then conditional lake calibration."""
from copy import deepcopy
import json
import os
import uuid

import numpy as np
import pandas as pd

from .calibration_config import FLOW_SELECTABLE_KEYS
from .calibration_search import DEFAULT_BUDGETS, DEFAULT_SEEDS, run_fit
from .common import digest, read_json, write_json, sha_file, within
from .fast_calibration import DujaEvaluator
from .grid_simulation import prepare as prepare_model, run as simulate, model_identity
from .lake_calibration import response as lake_response, fit as lake_fit, assess as lake_assess
from .model_core import normalize_initial_hydrology


def defaults():
    return {'version':1,'mode':'both','reverseCheck':True,'finalRefit':True,
            'seeds':list(DEFAULT_SEEDS),'budgets':dict(DEFAULT_BUDGETS),'allowExtension':True,
            'selectionMode':'flow-only','candidateSourceTaskId':'JOB-4934a5d35c9344a59353',
            'baselineTaskId':None,'flowConstraints':{'minNse':.70,'maxAbsPbias':5.},
            'candidateLimit':256,'jointBudget':2000,
            'lakePeriods':{'trainingStart':'2021-04-23T00:00:00','trainingEnd':'2025-01-01T00:00:00',
                           'evaluationStart':'2025-01-01T00:00:00','evaluationEnd':'2026-09-14T00:00:00'}}


def validate_options(options):
    value={**defaults(),**deepcopy(options or {})}
    if value['mode'] not in ('both','flow','lake'):
        raise ValueError('请选择两阶段、流量率定或湖位率定')
    seeds=value['seeds']
    if not isinstance(seeds,list) or not 1<=len(seeds)<=4 or len(set(seeds))!=len(seeds) or any(type(s)!=int or not 0<=s<2**32 for s in seeds):
        raise ValueError('请填写1—4个不同的搜索起点种子')
    budgets=value['budgets']
    if set(budgets)!=set(DEFAULT_BUDGETS) or any(type(b)!=int or not 1<=b<=12000 for b in budgets.values()):
        raise ValueError('各组搜索次数须在1—12000之间')
    if sum(budgets.values())>36000:
        raise ValueError('基础搜索总次数最多36000次')
    for key in ('reverseCheck','finalRefit','allowExtension'):
        if type(value[key])!=bool:
            raise ValueError('率定流程选项无效')
    if value['selectionMode'] not in ('flow-only','flow-constrained-lake','continuous-dual'):
        raise ValueError('请选择有效的共享参数筛选方式')
    continuous=value['selectionMode']=='continuous-dual'
    if continuous:
        from .calibration_objective import validate_objective
        value['objective']=validate_objective(value.get('objective'))
        value['flowConstraints']=None
        value['candidateSourceTaskId']=None
        value['reverseCheck']=False
    if not continuous and (not isinstance(value['candidateSourceTaskId'],str) or not value['candidateSourceTaskId'].strip()):
        raise ValueError('参考候选任务编号无效')
    if value['baselineTaskId'] is not None and (not isinstance(value['baselineTaskId'],str) or not value['baselineTaskId'].strip()):
        raise ValueError('基准成果编号无效')
    constraints=value['flowConstraints']
    if not continuous and (not isinstance(constraints,dict) or set(constraints)!={'minNse','maxAbsPbias'} or
            any(isinstance(constraints[k],bool) or not isinstance(constraints[k],(int,float)) or
                not np.isfinite(constraints[k]) for k in constraints) or
            not -1<=constraints['minNse']<=1 or not 0<=constraints['maxAbsPbias']<=100):
        raise ValueError('杜加流量门限无效')
    if type(value['candidateLimit'])!=int or not 1<=value['candidateLimit']<=256:
        raise ValueError('候选数量须在1—256之间')
    if type(value['jointBudget'])!=int or not 0<=value['jointBudget']<=2000:
        raise ValueError('联合评价次数须在0—2000之间')
    dates={k:pd.Timestamp(value['lakePeriods'][k]) for k in ('trainingStart','trainingEnd','evaluationStart','evaluationEnd')}
    if any(pd.isna(t) or t.tzinfo or t!=t.normalize() for t in dates.values()):
        raise ValueError('湖位时段须为北京时间完整日')
    if not dates['trainingStart']<dates['trainingEnd']<=dates['evaluationStart']<dates['evaluationEnd']:
        raise ValueError('湖位率定期与检验期应依次排列')
    return value


def prepare(db,payload):
    from .research_schemes import prepare_request
    prepared=prepare_request(db,payload,kind='simulation')
    options=validate_options(prepared['schemeSnapshot'].get('twoStage'))
    if payload.get('workflowMode'):
        options['mode']=payload['workflowMode']
        options=validate_options(options)
    if prepared['parameterTransfer']['mode']!='duja-shared' or prepared['partitionParameters']:
        raise ValueError('两阶段流程采用流域共享参数；请从当前采用方案新建')
    if set(prepared['search']['keys'])-set(FLOW_SELECTABLE_KEYS):
        raise ValueError('流量阶段只调整水文参数')
    product=next(r for r in db.items('input_products') if r['id']==prepared['inputId'])
    last=pd.Timestamp(product['manifest']['end']).normalize()
    if payload.get('end'):
        last=pd.Timestamp(payload['end'])
    if last<=pd.Timestamp(options['lakePeriods']['trainingStart']):
        raise ValueError('气象资料没有覆盖所选湖位时期')
    if pd.Timestamp(options['lakePeriods']['evaluationEnd'])>last:
        raise ValueError('湖位检验期超出完整气象覆盖')
    prepared.update(twoStage=options,end=last.isoformat(),evaluationStart=options['lakePeriods']['trainingStart'],
                    evaluationEnd=last.isoformat(),purpose='business')
    prepared['parameters']=deepcopy(prepared['schemeSnapshot'].get('calibrationInitialParameters',prepared['parameters'])) if options['mode']!='lake' else prepared['parameters']
    if options['mode']=='both' and options['selectionMode'] in ('flow-constrained-lake','continuous-dual'):
        from .business_selection import current
        from .lake_constrained_calibration import candidate_source
        if not prepared['search']['keys']:
            raise ValueError('湖位联合筛选需要选择水文参数')
        if options['selectionMode']=='flow-constrained-lake':
            _,path=candidate_source(db,options['candidateSourceTaskId'])
            prepared['_candidateSourceTraceSHA256']=sha_file(path)
        baseline=options.get('baselineTaskId') or current(db).get('resultId')
        if baseline:
            task=db.task(baseline)
            if task.get('status')!='succeeded' or not (task.get('result') or {}).get('daily'):
                raise ValueError('对照基准须是已有湖位过程的成功成果')
        options['baselineTaskId']=baseline
    if options['mode']=='lake':
        from .business_selection import current
        base_id=payload.get('lakeOnlyBaseTaskId') or current(db).get('resultId')
        if base_id:
            base=db.task(base_id);result=base.get('result') or {}
            land=lambda p:{k:v for k,v in p.items() if k!='lakeCoefficient'}
            compatible=(base['status']=='succeeded' and result.get('inputId')==prepared['inputId'] and
                result.get('spatialVersion')==prepared['spatialVersion'] and
                result.get('start')==prepared['start'] and result.get('end')==prepared['end'] and
                land(result.get('parameters',{}))==land(prepared['parameters']) and
                result.get('partitionParameters',{})==prepared['partitionParameters'] and
                result.get('parameterTransfer')==prepared['parameterTransfer'] and
                normalize_initial_hydrology(result.get('initialHydrology'))==
                    normalize_initial_hydrology(prepared.get('initialHydrology')) and
                result.get('routingMode')==prepared['routingMode'])
            if compatible:
                prepared['lakeOnlyBaseTaskId']=base_id
                prepared['_fixedHydrologySource']={**deepcopy(result.get('parameterSources',{}).get('hydrology',
                    {'method':'fixed-successful-result-parameters'})),'sourceTaskId':base_id}
            elif payload.get('lakeOnlyBaseTaskId'):
                raise ValueError('所选来水成果与当前水文参数、空间或时段不一致')
    return prepared


def run(db,request,folder,progress,cancelled):
    source=prepare_model(db,request['inputId'],request['spatialVersion'])
    options=request['twoStage']
    initial=deepcopy(request['parameters'])
    flow_records=[]
    selected=deepcopy(initial)
    state_path=folder/'pipeline-state.json'
    state=read_json(state_path,{'phase':'flow','completed':[]})
    fits=[]
    periods=request['periods']
    continuous=options.get('selectionMode')=='continuous-dual'
    joint=options['mode']=='both' and options.get('selectionMode') in ('flow-constrained-lake','continuous-dual')
    selection=None
    if joint:
        if continuous:
            from .dual_calibration import run_selection
        else:
            from .lake_constrained_calibration import run_selection
        state.update(phase='joint',fit='joint')
        write_json(state_path,state)
        selection=run_selection(db,request,source,folder/'joint',progress,cancelled)
        selected=deepcopy(selection['parameters'])
        fit={'fit':'共享参数 · 杜加全部资料',
             'algorithm':'historic-candidates-rescored-and-lake-constrained',
             'parameters':deepcopy(selected),
             'initialParameters':deepcopy(initial),
             'bounds':{key:deepcopy(request['search']['bounds'][key]) for key in request['search']['keys']},
             'seeds':list(options['seeds']),
             'trainingPeriod':{'start':min(periods['trainingStart'],periods['evaluationStart']),
                               'end':max(periods['trainingEnd'],periods['evaluationEnd'])},
             'validationPeriod':{'start':None,'end':None},
             'training':selection['flowMetrics']['all'],'validation':None,
             'yearlyEvaluation':selection['flowMetrics'],
             'evaluations':selection['totalRescored']+selection['refinement']['evaluations'],
             'bestObjective':selection['flowObjective'],
             'converged':selection['refinement']['converged'],
             'termination':selection['refinement']['termination'],
             'validationExcludedFromSearch':False}
        flow_records.extend(selection['flowFits'] if continuous else [fit])
        state['completed']=['joint']
        write_json(state_path,state)
    elif options['mode']!='lake':
        def period_label(start,end):
            first=pd.Timestamp(start).year;last=(pd.Timestamp(end)-pd.Timedelta(days=1)).year
            return str(first) if first==last else '%d—%d'%(first,last)
        training_label=period_label(periods['trainingStart'],periods['trainingEnd'])
        evaluation_label=period_label(periods['evaluationStart'],periods['evaluationEnd'])
        fits=[('forward',training_label+'→'+evaluation_label,periods['trainingStart'],periods['trainingEnd'],periods['evaluationStart'],periods['evaluationEnd'])]
        if options['reverseCheck']:
            fits.append(('reverse',evaluation_label+'→'+training_label,periods['evaluationStart'],periods['evaluationEnd'],periods['trainingStart'],periods['trainingEnd']))
        if options['finalRefit']:
            fits.append(('all-data','全部流量资料',min(periods['trainingStart'],periods['evaluationStart']),max(periods['trainingEnd'],periods['evaluationEnd']),None,None))
        evaluator=DujaEvaluator(source,request,end=max(periods['trainingEnd'],periods['evaluationEnd']))
        proof_path=folder/'fast-parity.json'
        if not proof_path.is_file():
            proof=[]
            for candidate in (initial,{**initial,'WM':min(300.,initial['WM']+5.),
                                      'lapseRate':max(-.01,initial['lapseRate']-.0002),
                                      'tauHours':min(168.,initial['tauHours']+3.)}):
                reference=simulate(db,{**request,'end':evaluator.times[-1].isoformat(),
                    'parameters':candidate,'_metricsOnly':True},prepared=source,cancelled=cancelled)
                frame=pd.DataFrame(reference['daily']);frame.index=pd.to_datetime(frame.date)
                a=frame.duja.reindex(evaluator.days).to_numpy();b=evaluator.daily(candidate)
                valid=np.isfinite(a)&np.isfinite(b)
                error=float(np.max(abs(a[valid]-b[valid])))
                if error>1e-7 or not np.array_equal(np.isfinite(a),np.isfinite(b)):
                    raise ValueError('杜加快速评价与完整模拟不一致')
                proof.append({'parameters':candidate,'maximumDischargeDifferenceM3s':error,'pairedDays':int(valid.sum())})
            write_json(proof_path,{'modelImplementation':request['_modelIdentity'],
                                  'fastImplementationSHA256':sha_file(__import__('backend.fast_calibration',fromlist=['__file__']).__file__),
                                  'comparisons':proof})
        for index,(name,label,train_start,train_end,val_start,val_end) in enumerate(fits):
            state.update(phase='flow',fit=name,fitIndex=index,fitCount=len(fits))
            write_json(state_path,state)
            def report(p,message):
                progress((index+p/100)/len(fits)*82,message)
            report_row=run_fit(evaluator,request,folder/name,label,train_start,train_end,val_start,val_end,
                               initial,report,cancelled,candidates=[r['parameters'] for r in flow_records] if name=='all-data' else [])
            flow_records.append(report_row)
            selected=report_row['parameters'] if name in ('forward','all-data') else selected
            state['completed']=[r[0] for r in fits[:index+1]]
            write_json(state_path,state)
    working={**deepcopy(request),'parameters':selected,'partitionParameters':{},'_metricsOnly':False}
    # All later lake calculations reuse this fixed basin run. The observation
    # anchor was captured once by Jobs before either stage started.
    baseline_folder=within(folder,state.get('baselineFolder','land-baseline'))
    baseline=read_json(baseline_folder/'result.json')
    if baseline is None:
        if baseline_folder.exists():
            baseline_folder=folder/('land-baseline-'+uuid.uuid4().hex[:8])
        baseline_folder.mkdir()
        state['baselineFolder']=baseline_folder.name
        state.update(phase='basin-simulation')
        write_json(state_path,state)
        land_reuse=None
        if options['mode']=='lake' and request.get('lakeOnlyBaseTaskId'):
            base=db.task(request['lakeOnlyBaseTaskId'])
            land_reuse=(within(db.workspace/'tasks',base['id']),base['result'])
        baseline=simulate(db,working,baseline_folder,lambda p,m:progress(82+p*.05,m),cancelled,source,
                          land_reuse=land_reuse)
    else:
        if baseline['parameters']!=selected or baseline['identity']['modelImplementation']!=request['_modelIdentity']:
            raise ValueError('接续时全流域来水基准与率定参数不一致')
    if joint:
        from .lake_constrained_calibration import verify_formal_result
        selection['formalParity']=verify_formal_result(selection,request,baseline,folder/'joint')
    lake_record=None
    if options['mode']!='flow':
        progress(88,'湖泊参数率定 · 固定入湖来水')
        state.update(phase='lake')
        write_json(state_path,state)
        if joint:
            split,final=selection['trainingFit'],selection['finalFit']
        else:
            frame=lake_response(source,working,baseline)
            lake_periods=options['lakePeriods']
            bounds=request['search']['bounds']['lakeCoefficient']
            coeff=selected['lakeCoefficient']
            anchor=baseline['initialization']
            split=lake_fit(frame,coeff,bounds,lake_periods['trainingStart'],lake_periods['trainingEnd'],anchor)
            split['validation']=lake_assess(frame,split['value'],coeff,lake_periods['evaluationStart'],lake_periods['evaluationEnd'],anchor)
            split['validationPeriod']={'start':lake_periods['evaluationStart'],'end':lake_periods['evaluationEnd']}
            final=lake_fit(frame,coeff,bounds,lake_periods['trainingStart'],lake_periods['evaluationEnd'],anchor) if options['finalRefit'] else split
        lake_record={'split':split,'final':final,'hydrologicalParametersFixed':True,
                     'hydrologicalParameterSHA256':digest({k:v for k,v in selected.items() if k!='lakeCoefficient'}),
                     'usedForFlowSelection':joint}
        if joint:
            lake_record['splitHydrologicalParameterSHA256']=digest({k:v for k,v in selection['trainingParameters'].items() if k!='lakeCoefficient'})
            lake_record['splitAndFinalHydrologyMayDiffer']=True
        selected={**selected,'lakeCoefficient':final['value']}
        write_json(folder/'lake-fit.json',lake_record)
    if cancelled():
        raise InterruptedError('已停止，率定结果与来水基准已保存，可继续')
    state.update(phase='final-simulation')
    if continuous and joint and baseline['parameters']==selected:
        # Joint selection already supplied the final conditional lake value.
        # Its full baseline is the required formal run, including all states.
        state['finalFolder']=state['baselineFolder']
    write_json(state_path,state)
    working['parameters']=selected
    output_folder=within(folder,state.get('finalFolder','final-output'))
    final=read_json(output_folder/'result.json')
    if final is None:
        if output_folder.exists():output_folder=folder/('final-output-'+uuid.uuid4().hex[:8])
        output_folder.mkdir()
        state['finalFolder']=output_folder.name
        write_json(state_path,state)
        final=simulate(db,working,output_folder,lambda p,m:progress(90+p*.09,m),cancelled,source,
                       land_reuse=(baseline_folder,baseline))
    if joint:
        from .warmup_workflow import run as warmup_check
        state.update(phase='warmup-diagnostics')
        write_json(state_path,state)
        progress(98.,'最终参数预热核验')
        final['warmupDiagnostics']=warmup_check(db,request,source,final,folder/'warmup-diagnostics',progress,cancelled)
    # Completed artifacts are linked into the normal task layout. Unfinished
    # attempts stay in their own folders; Writer never clears an old output.
    for source_path in output_folder.rglob('*'):
        if not source_path.is_file() or source_path.name=='result.json':continue
        target=folder/source_path.relative_to(output_folder)
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():
            if sha_file(target)!=sha_file(source_path):
                raise ValueError('已有任务工件与完成输出不一致：'+target.name)
        else:
            os.link(source_path,target)
    final['calibrationWorkflow']={'version':1,'target':'two-stage','mode':options['mode'],
        'flowFits':flow_records,'lake':lake_record,'finalRefit':options['finalRefit'],
        'parametersSharedAcrossLand':True,
        'trainingDataUse':(('lake-training-selection-then-all-data-refit' if options['finalRefit'] else 'lake-training-selection-with-time-check')
                           if joint else ('split-checks-then-all-data-refit' if options['finalRefit'] else 'time-split-checks')),
        'flowParameterSelectionUsesLakeLevel':joint,'selectionMode':options.get('selectionMode','flow-only') if joint else 'flow-only',
        'stationAfterRouting':'Duja included once within Zajia; existing partition routing retained'}
    if joint:
        final['calibrationWorkflow']['selection']=selection
    final['calibration']={'algorithm':('fresh-grouped-continuous-dual-v1' if continuous else 'flow-constrained-lake-v1') if joint else 'flow-then-lake-v1','evaluations':sum(r['evaluations'] for r in flow_records),
        'stages':[{'stage':r['fit'],'evaluations':r['evaluations'],'bestObjective':r['bestObjective'],
                   'converged':r['converged'],'termination':r['termination']} for r in flow_records],
        'seeds':options['seeds'],'trace':[],'traceExternal':True,'objectiveProfile':request['search']['objectiveProfile'],
        'converged':(not flow_records or flow_records[-1]['converged']) and (lake_record is None or lake_record['final']['converged']),
        'termination':'completed-with-recorded-search-status',
        'bestObjective':flow_records[-1]['bestObjective'] if flow_records else lake_record['final']['training']['rmse']}
    hydrology_source=({'method':(('continuous-flow-lake-calibration' if continuous else 'Duja-flow-constrained-lake-selection') if joint else
                                  'Duja-flow-all-data-refit' if options['finalRefit'] else 'Duja-flow-split-fit'),
                       'keys':request['search']['keys'],'periods':periods} if flow_records else
                      deepcopy(request.get('_fixedHydrologySource') or request['schemeSnapshot'].get('parameterSources',{}).get('hydrology',
                        {'method':'fixed-scheme-parameters','sourceSchemeId':request['researchSchemeId']})))
    final['parameterSources']={'hydrology':hydrology_source,
                               'lake':{'method':('conditional-lake-four-criterion-fit' if joint else 'conditional-lake-level-fit') if lake_record else 'fixed','keys':['lakeCoefficient']},
                               'fixed':['iceSublimationFraction','iceSublimationMaxMmHour']}
    final['diagnostics']['lakeCumulativeBalanceErrorM3']=float(sum(row.get('lakeBalanceErrorM3') or 0 for row in final['daily']))
    state.update(phase='completed')
    write_json(state_path,state)
    write_json(folder/'result.json',final)
    return final


def search_progress(folder,offset=0,limit=200):
    state=read_json(folder/'pipeline-state.json',{})
    current=state.get('fit')
    if not current:
        return {'trace':[],'total':0,'workflow':state}
    subfolder=folder/current
    summary=read_json(subfolder/'search-progress.json',{})
    path=subfolder/'search-trace.jsonl'
    records={}
    # Keep the final entry for any evaluation replayed after an abrupt restart.
    if path.is_file():
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                try:row=json.loads(line)
                except json.JSONDecodeError:continue
                if row['evaluation']<=summary.get('traceCount',0):records[row['evaluation']]=row
    values=[records[k] for k in sorted(records)]
    return {**summary,'trace':values[offset:offset+limit],'total':len(values),'offset':offset,
            'workflow':state,'hasMore':offset+limit<len(values)}
