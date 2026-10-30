"""Write a small immutable receipt for the already generated comparison."""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.common import ROOT,canonical,digest,sha_file
from backend.database import Database
from backend.fast_calibration import DujaEvaluator
from backend.grid_simulation import model_identity,prepare
from backend.grid_storage import open_arrays
from backend.lake_calibration import freeze_window,joint_assess

TASK='JOB-3354c8660e854cbfa96f'
BASE='JOB-88b35b44b27d4e15840c'
DEST=ROOT/'docs/evidence/站点暖雨固定参数对照-整点修正-20261030.json'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--task',default=TASK,help='要核验的独立对照任务')
    parser.add_argument('--output',default=str(DEST),help='独立证据文件；已有文件不会覆盖')
    args=parser.parse_args()
    task_id=args.task
    destination=Path(args.output)
    db=Database();task=db.task(task_id);req=task['request'];result=task['result'];base=db.task(BASE)['result']
    folder=db.workspace/'tasks'/task_id;report_path=folder/'站点暖雨对照报告.json'
    report=json.loads(report_path.read_text(encoding='utf-8'))
    saved_manifest=json.loads((folder/'hourly-manifest.json').read_text(encoding='utf-8'))
    bad=[name for name,sha in saved_manifest['files'].items()
         if not (folder/'hourly.zarr'/name).is_file() or sha_file(folder/'hourly.zarr'/name)!=sha]
    if bad:raise ValueError('逐时工件摘要不匹配：'+str(bad[:3]))
    if saved_manifest['sha256']!=digest(saved_manifest['files']) or result['hourlyArtifact']['sha256']!=saved_manifest['sha256']:
        raise ValueError('逐时工件总摘要不匹配')
    source=prepare(db,req['inputId'],req['spatialVersion'])
    fast=DujaEvaluator(source,{**req,'search':{}})
    fast_daily=pd.Series(fast.daily(req['parameters']),index=fast.days)
    rows=pd.DataFrame(result['daily']).set_index('date');rows.index=pd.to_datetime(rows.index)
    fast_daily=fast_daily.reindex(rows.index);valid=fast_daily.notna()&rows.duja.notna()
    fast_max=float((fast_daily[valid]-rows.duja[valid]).abs().max())
    artifact=open_arrays(folder);flow=artifact['flow']
    river_max=max(float(np.max(np.abs(block[:,6]-np.sum(block[:,1:6],axis=1))))
                  for i in range(0,flow.shape[0],168) for block in [np.asarray(flow[i:i+168])])
    actual_source=Path(db.workspace/'inputs/products'/report['baselineInputId']/'forcing.npz')
    corrected_source=Path(db.workspace/'inputs/products'/report['comparisonInputId']/'forcing.npz')
    source_sha,corrected_sha=sha_file(actual_source),sha_file(corrected_source)
    parent=next(x['manifest'] for x in db.items('input_products') if x['id']==report['baselineInputId'])
    derived=next(x['manifest'] for x in db.items('input_products') if x['id']==report['comparisonInputId'])
    same_lake=(parent['lakePrecipitationCorrection']==derived['lakePrecipitationCorrection'] and
               parent['transformationSHA256']==derived['transformationSHA256'])
    if not same_lake or source_sha!=corrected_sha or source_sha!=parent['dataSHA256']:
        raise ValueError('原始气象或湖面降水身份不一致')
    baseline=json.loads((folder/'原气象固定参数日过程.json').read_text(encoding='utf-8'))
    left=pd.DataFrame(baseline['daily']).set_index('date');left.index=pd.to_datetime(left.index)
    original=pd.DataFrame(base['daily']).set_index('date');original.index=pd.to_datetime(original.index)
    reproducible={}
    for key in ('duja','total','lakeLevel','lakeRainMm','lakeEvaporationMm'):
        pair=pd.concat([left[key],original[key]],axis=1).dropna()
        reproducible[key]=float((pair.iloc[:,0]-pair.iloc[:,1]).abs().max()) if len(pair) else None
    window=freeze_window(req['_observations']['level'],'2021-04-23','2026-09-14',req['_fixedInitialization'])
    scores={}
    for name,frame in [('baseline',left),('corrected',rows)]:
        f=frame.copy();f['baseLevel']=f['lakeLevel'];f['slope']=0.
        scores[name]=joint_assess(f,0.,0.,window)
    adoption=db.setting('business-adopted-scheme-v1');forcing=db.setting('current-model-forcing')
    if adoption['resultId']!=BASE or forcing['inputId']!=report['baselineInputId']:
        raise ValueError('当前默认采用关系已变化')
    receipt={'schemaVersion':1,'taskId':task_id,'baselineTaskId':BASE,
        'inputId':report['comparisonInputId'],'baselineInputId':report['baselineInputId'],
        'parameterId':report['parameterId'],'parameterSHA256':report['parametersSHA256'],
        'landTransformationSHA256':report['landTransformationSHA256'],
        'lakeTransformationSHA256':report['lakeTransformationSHA256'],'forcingDataSHA256':source_sha,
        'modelIdentityAtRun':report['modelImplementation'],'modelIdentityAtVerification':model_identity(),
        'reportFile':report_path.relative_to(ROOT).as_posix(),'reportSHA256':sha_file(report_path),
        'trialResultSHA256':sha_file(folder/'result.json'),
        'checks':{'hourlyFiles':len(saved_manifest['files']),'hourlyMismatchCount':len(bad),
            'hourlyManifestSHA256':saved_manifest['sha256'],'fastDays':int(valid.sum()),
            'fastVsCompleteMaxM3s':fast_max,'riverSumMaxM3s':river_max,
            'landBalanceMaxMm':result['diagnostics']['landGlacierMaxBalanceErrorMm'],
            'routingBalanceMaxM3':result['diagnostics']['routingMaxBalanceErrorM3'],
            'sameLakeRule':same_lake,'sameForcingBytes':source_sha==corrected_sha,
            'baselineVsAdoptedDailyMaxAbs':reproducible},
        'scores':scores,'comparison':report['fixedParameterComparison'],
        'stationDiagnostics':report['stationDiagnostics'],'stationStatistics':report['stationStatistics'],
        'eventTemperaturePairing':report['profile'].get('eventTemperaturePairing'),
        'confirmedSources':report['profile']['confirmedEventSources'],
        'confirmedCoverage':'3站2025整年及2026-01-01至10-20；2024逐次未确认',
        'defaultUnchanged':True,'adopted':False,
        'interpretation':['暖时是ERA气温代理，缺少站点气温和独立降雪量',
            '最终订正使用2024—2026合格站季，2017—2023是时间外推',
            '2025/2026已参与开发，时间比较不能称盲验证','未利用Q或H选择站雨因子或半径','未重新率定或采用']}
    text=json.dumps(receipt,ensure_ascii=False,indent=2,allow_nan=False)+'\n'
    destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.exists():
        if destination.read_text(encoding='utf-8')!=text:raise ValueError('证据文件已存在且内容不同，不覆盖')
    else:
        with destination.open('x',encoding='utf-8',newline='\n') as f:f.write(text)
    print('证据',destination,'摘要',sha_file(destination),'暖雨',report['comparisonInputId'],'结果',task_id,flush=True)


if __name__=='__main__':main()
