"""Read frozen results and write reproducible two-stage delivery evidence."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.common import ROOT, WORKSPACE, digest, read_json, sha_file, write_json
from backend.fast_calibration import array_metrics
from backend.grid_spatial import load
from backend.grid_storage import open_arrays
from backend.lake_calibration import assess as lake_assess
from backend.simulation import RIVERS


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--task',default='JOB-4934a5d35c9344a59353')
    parser.add_argument('--output',required=True)
    parser.add_argument('--configuration-output')
    args=parser.parse_args()
    output=ROOT/args.output
    if output.exists():raise ValueError('请使用新的核验输出路径')
    connection=sqlite3.connect('file:'+str(WORKSPACE/'database/hydrology.sqlite3')+'?mode=ro',uri=True)
    connection.row_factory=sqlite3.Row
    def task(identifier):
        value=dict(connection.execute('SELECT * FROM tasks WHERE id=?',(identifier,)).fetchone())
        assert value['status']=='succeeded',(identifier,value['status'],value['message'])
        value['result']=json.loads(value['result']);value['request']=json.loads(value['request'])
        return value
    current=task(args.task)
    result=current['result'];request=current['request'];folder=WORKSPACE/'tasks'/args.task
    workflow=result['calibrationWorkflow'];spatial=load(result['spatialVersion'])
    frame=pd.DataFrame(result['daily']);frame.index=pd.to_datetime(frame.date)
    assert len(workflow['flowFits'])==3
    assert workflow['flowFits'][-1]['training']['count']==440
    assert not workflow['flowParameterSelectionUsesLakeLevel']
    assert result['parameterTransfer']['mode']=='duja-shared'
    assert not result['partitionParameters']
    assert not set(u['partition'] for u in result['units']) & {'unresolved','external'}
    for row in result['parameterTransferAudit']['effective']['regions'].values():
        assert row['parameters']==result['parameters']
    baseline=read_json(folder/read_json(folder/'pipeline-state.json')['baselineFolder']/'result.json')
    final_hydrology={k:v for k,v in result['parameters'].items() if k!='lakeCoefficient'}
    assert digest(final_hydrology)==workflow['lake']['hydrologicalParameterSHA256']
    assert final_hydrology=={k:v for k,v in baseline['parameters'].items() if k!='lakeCoefficient'}
    flow_keys=['duja','zajia','zagen','boqu','ali','fringe','total']
    assert pd.DataFrame(baseline['daily'])[flow_keys].equals(frame.reset_index(drop=True)[flow_keys])
    artifacts=read_json(folder/'hourly-manifest.json')
    for name,expected in artifacts['files'].items():
        assert sha_file(folder/'hourly.zarr'/name)==expected,name
    hourly=open_arrays(folder)
    sums=np.zeros(len(RIVERS));max_aggregation=0.;excluded_max=0.
    for start in range(0,len(hourly['time']),168):
        values=np.asarray(hourly['flow'][start:start+168]);sums+=values.sum(axis=0)*3600
        expected=sum(values[:,RIVERS.index(k)] for k in ('zajia','zagen','boqu','ali','fringe'))
        max_aggregation=max(max_aggregation,float(np.max(abs(values[:,RIVERS.index('total')]-expected))))
        excluded_max=max(excluded_max,float(np.max(abs(np.asarray(hourly['excludedRunoff'][start:start+168])))))
    assert max_aggregation<1e-9 and excluded_max==0.
    station_sum=sum(result['diagnostics']['partitions'][p]['routedM3'] for p in ('duja','zajia_rest'))
    station_error=float(sums[RIVERS.index('zajia')]-station_sum)
    assert abs(station_error)<.01
    lake_residual=float(frame.lakeBalanceErrorM3.sum())
    assert abs(lake_residual)<.01
    anchor_day=result['initialization']['time'][:10]
    assert result['displayCoverage']['start'][:10]=='2021-04-23'
    assert frame.loc[frame.lakeLevel.notna()].index[0].date().isoformat()=='2021-04-24'
    assert result['displayCoverage']['endExclusive']=='2026-10-21T00:00:00'
    assert result['end']=='2026-10-21T00:00:00'
    searches=[]
    for subfolder in ('forward','reverse','all-data'):
        fit=read_json(folder/subfolder/'fit-result.json')
        state=read_json(folder/subfolder/'optimizer-state.json')
        trace=folder/subfolder/'search-trace.jsonl'
        assert sha_file(trace)==fit['traceSHA256']
        records={};raw_count=0
        with trace.open(encoding='utf-8') as stream:
            for line in stream:
                row=json.loads(line);records[row['evaluation']]=row;raw_count+=1
        assert sorted(records)==list(range(1,fit['evaluations']+1))
        assert state['evaluations']==fit['evaluations']
        assert state['active'] is None
        assert fit['seeds']==[42,142,242]
        assert len(fit['bounds'])==22
        assert fit['validationExcludedFromSearch']
        assert fit['training']['count']==len(fit['fixedTrainingDates'])
        train=fit['trainingPeriod'];validation=fit['validationPeriod']
        if validation['start']:
            assert all(not validation['start']<=d<validation['end'] for d in fit['fixedTrainingDates'])
        searches.append({k:deepcopy(fit[k]) for k in ('fit','identity','algorithm','evaluations','extended','seeds',
            'bestObjective','converged','termination','nearBoundaryParameters','trainingPeriod','validationPeriod',
            'training','validation','bounds','initialParameters','parameters','traceSHA256','fixedTrainingObservationSHA256')})
        searches[-1].update(traceLines=raw_count,uniqueEvaluations=len(records),traceFile=str(trace.relative_to(ROOT)),
                            stateSHA256=sha_file(folder/subfolder/'optimizer-state.json'))
        populations=read_json(folder/subfolder/'population-replay/manifest.json')
        assert populations['traceSHA256']==fit['traceSHA256']
        for population in populations['populations']:
            assert sha_file(folder/subfolder/'population-replay'/population['file'])==population['sha256']
        searches[-1]['populations']=populations
    def comparison(identifier):
        item=task(identifier);value=item['result'];daily=pd.DataFrame(value['daily']);daily.index=pd.to_datetime(daily.date)
        annual={}
        for year in (2024,2025):
            pairs=daily.loc[daily.index.year==year,['observedFlow','duja']].dropna()
            annual[str(year)]=array_metrics(pairs.observedFlow,pairs.duja)
        lake_frame=daily.assign(baseLevel=daily.lakeLevel,slope=0.)
        anchor=value['initialization'];coefficient=value['parameters']['lakeCoefficient']
        return {'taskId':identifier,'spatialVersion':value['spatialVersion'],'inputId':value['inputId'],
            'initialization':anchor,'parameters':value['parameters'],'partitionParameters':value.get('partitionParameters',{}),
            'flow':annual,'lake':{name:lake_assess(lake_frame,coefficient,coefficient,start,end,anchor)
                for name,start,end in [('common2023Onward','2023-04-21','2026-10-21'),
                                      ('timeCheck2025Onward','2025-01-01','2026-10-21'),
                                      ('availableSince2021','2021-04-23','2026-10-21')]}}
    selection=connection.execute('SELECT value FROM settings WHERE key=?',('business-adopted-scheme-v1',)).fetchone()
    adopted=json.loads(selection[0]) if selection else {}
    assert adopted.get('resultId')==args.task,adopted
    report={'taskId':args.task,'resultSHA256':sha_file(folder/'result.json'),'requestSHA256':sha_file(folder/'request.json'),
        'adoption':adopted,'calculationDomain':spatial['calculationDomain'],'displayCoverage':result['displayCoverage'],
        'initialization':result['initialization'],'parameters':result['parameters'],'flowFits':searches,'lake':workflow['lake'],
        'comparisonInterpretation':'Old adopted result uses a 2023 anchor and legacy spatial/parameters; A1 is the previous 2021 shared reference. This round also changes domain and recalibrates parameters; comparisons are not single-factor attribution. 2025/2026 were previously inspected.',
        'comparisons':{'oldAdopted':comparison('JOB-f671b2a7af4d472cb70b'),
                       'previousShared2021':comparison('JOB-67af4fecb3fa44d39619'),'twoStage':comparison(args.task)},
        'checks':{'sharedParameterRegions':len(result['parameterTransferAudit']['effective']['regions']),
                  'calculationUnits':len(result['units']),'hourlyArtifactsVerified':len(artifacts['files']),
                  'lakeStageFlowUnchanged':True,'excludedBackgroundRunoffM3':excluded_max,
                  'maximumTotalFlowSummationDifferenceM3s':max_aggregation,'dujaIncludedOnceVolumeErrorM3':station_error,
                  'cumulativeLakeBalanceErrorM3':lake_residual,'firstCompleteLakeDay':'2021-04-24',
                  'searchSplitMasksVerified':True,'allDataFlowDays':440},
        'numericalDiagnostics':result['diagnostics'],'fastParity':read_json(folder/'fast-parity.json'),
        'implementationSnapshot':read_json(folder/'implementation-snapshot/manifest.json')}
    write_json(output,report)
    if args.configuration_output:
        from backend.model_core import PARAMETER_DEFINITIONS,DEFAULT_PARAMETERS
        from backend.calibration_config import LAND_KEYS,SNOW_KEYS,ROUTING_KEYS
        path=ROOT/args.configuration_output
        if path.exists():raise ValueError('方案导出已存在，请使用新路径')
        saved=connection.execute('SELECT payload FROM schemes WHERE id=?',(adopted['schemeId'],)).fetchone()
        inventory=[]
        for definition in PARAMETER_DEFINITIONS:
            key=definition['key'];stage=('runoff' if key in LAND_KEYS else 'snow' if key in SNOW_KEYS
                else 'routing' if key in ROUTING_KEYS else 'lake' if key=='lakeCoefficient' else None)
            inventory.append({'key':key,'meaning':definition['label'],'unit':definition['unit'],
                'defaultValue':DEFAULT_PARAMETERS[key],'initialValue':request['parameters'][key],
                'value':result['parameters'][key],'physicalBounds':[definition['min'],definition['max']],
                'searchBounds':request['search']['bounds'][key],'optimizationStage':stage,
                'applicableRegion':'lake' if stage=='lake' else 'all-calculation-land',
                'sourceTaskId':args.task,'source':'two-stage-calibration' if stage else 'unchanged-manual-prior'})
        write_json(path,{'schemaVersion':1,'adoption':adopted,'payload':json.loads(saved[0]),
            'parameterSources':result['parameterSources'],'parameterInventory':inventory,
            'forcingReference':{'inputId':result['inputId'],'firstCompleteDay':'2019-12-31','lastCompleteDay':'2026-10-20'},
            'spatialReference':{'version':spatial['version'],'manifestSHA256':digest(spatial),'domain':spatial['calculationDomain']},
            'resultReference':{'taskId':args.task,'resultSHA256':sha_file(folder/'result.json'),
                               'hourlyManifestSHA256':sha_file(folder/'hourly-manifest.json')},
            'evidenceFile':args.output,'fullArraysExternal':True})
    print(json.dumps({'output':str(output.relative_to(ROOT)),'task':args.task,'checks':report['checks']},ensure_ascii=True))


if __name__=='__main__':main()
