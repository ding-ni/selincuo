"""Promote one accepted development freeze and publish only its final products."""
import argparse
import sys
from pathlib import Path
import numpy as np
from copy import deepcopy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import read_json, write_json, digest, sha_file, now, model_identity
from backend.database import Database
from backend.forcing import register_lake_correction
from backend.operational_scheme import prepare_request
from backend.simulation import run_simulation


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--assessment',type=Path,required=True)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    freeze=read_json(args.assessment/'frozen-development.json')
    if digest({k:v for k,v in freeze.items() if k!='freezeSHA256'}) != freeze['freezeSHA256'] or not freeze['developmentAccepted']:
        raise ValueError('Development freeze missing, altered or rejected')
    args.output.mkdir(parents=True,exist_ok=False)
    db=Database()
    manifest=register_lake_correction(db,freeze['baselineIdentity']['inputId'],freeze['correction'])
    # Seed is constructed separately after final replay; never contaminate it with jobs.
    configuration=ROOT/'config/operational/historical-standard-v1.json'
    previous=read_json(configuration)
    write_json(args.output/'previous-scheme.json',previous)
    scheme=deepcopy(previous)
    scheme['revision']=2
    scheme['inputRequirement']={'transformationSHA256':manifest['transformationSHA256'],
                                'lakePrecipitationCorrection':freeze['correction']}
    scheme['parameters']['lakeCoefficient']=freeze['lakeCoefficient']
    scheme['evidence']['metrics2024']['lake']=freeze['candidate2024']
    scheme['evidence']['metrics2025']['purpose']='冻结条件下留出评价（开发曾查看）'
    scheme['evidence']['report']='docs/色林错标准方案与结果-v0.8.md'
    limitation='Lake precipitation spatial transfer from one shore gauge'
    if limitation not in scheme['evidence']['limitations']:
        scheme['evidence']['limitations'].append(limitation)
    scheme['parameterSource']['lakeRain']='2024 SLC gauge / collocated grid annual ratio; lake-only'
    write_json(configuration,scheme)
    request=prepare_request(db,{'schemeId':scheme['id'],'schemeRevision':2,'inputId':manifest['id'],
                               'start':'2024-01-01','end':'2026-01-01'})
    write_json(args.output/'request.json',request)
    result=run_simulation(db,request,args.output/'full',progress=lambda n,m:print(round(n,1),m,flush=True))
    with np.load(args.output/'full/hourly.npz') as actual, np.load(args.baseline/'hourly.npz') as baseline:
        for key in ('time','flow','unitDiagnostics','unitStates','routingStorage','sourceRoutingStorage'):
            np.testing.assert_array_equal(actual[key],baseline[key])
    for year in ('2024','2025'):
        expected=freeze['candidate2024'] if year=='2024' else read_json(args.assessment/'assessment.json')['evaluation2025']['candidate']
        if abs(result['metrics'][year]['lake']['rmse']-expected['rmse'])>1e-9:
            raise AssertionError('Full model replay differs from frozen lake response')
    if result['modelDiagnostics']['catchmentBalanceMaxErrorM3']>=.001:
        raise AssertionError('Catchment water accounting failed')
    # Check resume over the year boundary after replaying actual antecedent states.
    head=run_simulation(db,{**request,'end':'2024-12-31T17:00:00','evaluationEnd':'2024-12-31T00:00:00','_skipDiagnostics':True},args.output/'head')
    tail_request={k:v for k,v in request.items() if k not in ('evaluationStart','evaluationEnd')}
    tail=run_simulation(db,{**tail_request,'start':'2024-12-31T18:00:00','_parentState':head['checkpoint'],'_skipDiagnostics':True},args.output/'tail')
    if tail['checkpoint'] != result['checkpoint']:
        raise AssertionError('Resumed final checkpoint differs')
    with np.load(args.output/'full/hourly.npz') as full, np.load(args.output/'head/hourly.npz') as first, np.load(args.output/'tail/hourly.npz') as last:
        for key in ('flow','lakeLevel','volumeChange','unitStates','unitDiagnostics','continuousLakeVolumeChange'):
            np.testing.assert_allclose(full[key],np.concatenate([first[key],last[key]]),atol=1e-8,rtol=1e-12,equal_nan=True)
    scheme['evidence']['metrics2025']['lake']=result['metrics']['2025']['lake']
    write_json(configuration,scheme)
    # Refresh the scheme snapshot digest after attaching verified evidence.
    final_request=prepare_request(db,{'schemeId':scheme['id'],'start':'2024-01-01','end':'2026-01-01','inputId':manifest['id']})
    result['scheme']=final_request['schemeSnapshot']
    write_json(args.output/'full/result.json',result)
    suffix=model_identity()[:12]
    published_id='BUILTIN-historical-rev2-'+suffix
    dest=ROOT/('data/final-results/historical-rev2-'+suffix)
    if dest.exists():
        raise ValueError('Final product directory already exists; never overwrite a published result')
    dest.mkdir(parents=True)
    public={k:result[k] for k in ('projectId','resultSource','identity','start','end','hours','parameters','inputId',
                                'spatialVersion','metrics','daily','routingMode','evaluationPeriod','scheme','partitionParameters','units')}
    public['modelDiagnostics']={'partitions':result['modelDiagnostics']['partitions']}
    detail={'id':published_id,'builtin':True,'name':'色林错标准历史成果','status':'succeeded','progress':100,
            'request':{k:v for k,v in final_request.items() if not k.startswith('_')},'result':public}
    write_json(dest/'result.json',detail)
    (dest/'daily.csv').write_bytes((args.output/'full/daily.csv').read_bytes())
    write_json(dest/'final-state.json',result['checkpoint'])
    with np.load(args.output/'full/hourly.npz') as hourly:
        import pandas as pd
        all_times=pd.DatetimeIndex(hourly['time'].astype('datetime64[ns]'))
        mask=(all_times.hour==0)&(all_times>=pd.Timestamp('2024-01-02'))
        np.savez_compressed(dest/'daily-snow.npz',time=hourly['time'][mask],swe=hourly['unitStates'][mask,:,0])
    write_json(dest/'summary.json',{'scheme':result['scheme'],'identity':result['identity'], 'metrics':result['metrics'],
            'selectionYear':2024,'comparisonSource':freeze['baselineIdentity'],
            'lakeRainCorrection':freeze['correction'],'referenceOnly':True,
            'waterBalanceMaxErrorM3':result['modelDiagnostics']['catchmentBalanceMaxErrorM3'],
            'independentCryosphereValidation':False,'resumeVerified':True})
    files={p.name:{'sha256':sha_file(p),'bytes':p.stat().st_size} for p in dest.iterdir() if p.is_file()}
    write_json(ROOT/'data/final-results/catalog.json',{'results':[{'id':published_id,'name':detail['name'],
         'period':'2024—2025','directory':dest.name,'files':files}]})
    write_json(args.output/'verification.json',{'at':now(),'passed':True,'modelImplementation':model_identity(),
        'inputId':manifest['id'],'upstreamArrayEquality':True,'resumeVerified':True,
        'metrics':result['metrics'],'publishedId':published_id})
    print({'passed':True,'inputId':manifest['id'],'lake':{y:r['lake']['rmse'] for y,r in result['metrics'].items()}},flush=True)


if __name__=='__main__':main()
