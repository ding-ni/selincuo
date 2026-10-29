"""Create a uniquely named, auditable real-forcing warmup comparison.

This script reads the existing immutable request and observation snapshot. It
does not initialize Jobs, alter the workspace database, or change adoption.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
import uuid

import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.common import digest,sha_file
from backend.grid_simulation import model_identity,prepare,run
from backend.warmup_diagnostics import budget,comparison,snapshot


def write_new(path,value):
    with path.open('x',encoding='utf-8',newline='\n') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)
        stream.write('\n')


def jsonable(value):
    import numpy as np
    if isinstance(value,dict):return {k:jsonable(v) for k,v in value.items()}
    if isinstance(value,list):return [jsonable(v) for v in value]
    if isinstance(value,(np.integer,)):return int(value)
    if isinstance(value,(np.floating,)):return float(value) if np.isfinite(value) else None
    if isinstance(value,float) and not np.isfinite(value):return None
    return value


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--input-id',required=True)
    parser.add_argument('--workspace',default=str(ROOT/'build/grid-revision-20261026/workspace'))
    parser.add_argument('--base-task',default='JOB-4934a5d35c9344a59353')
    parser.add_argument('--parameter-result',help='可选：新成果result.json，复核采用后的参数')
    parser.add_argument('--starts',nargs='+',default=['2019-01-01','2020-01-01'])
    args=parser.parse_args();workspace=Path(args.workspace).resolve();base=workspace/'tasks'/args.base_task
    with (base/'request.json').open(encoding='utf-8') as stream:request=json.load(stream)
    with (base/'result.json').open(encoding='utf-8') as stream:result=json.load(stream)
    if args.parameter_result:
        with Path(args.parameter_result).open(encoding='utf-8') as stream:
            parameter_result=json.load(stream)
        result['parameters']=parameter_result['parameters']
    manifest_path=workspace/'inputs/products'/args.input_id/'manifest.json'
    with manifest_path.open(encoding='utf-8') as stream:manifest=json.load(stream)
    if min(pd.Timestamp(v) for v in args.starts)<pd.Timestamp(manifest['start']):
        raise ValueError('气象资料不覆盖所有预热起点')
    destination=workspace/'diagnostics'/('湖位预热核验-'+uuid.uuid4().hex[:12])
    destination.mkdir(parents=True,exist_ok=False)
    # The simulator only uses db.workspace when the input is already prepared.
    db=type('Workspace',(),{'workspace':workspace})()
    source=prepare(db,args.input_id,request['spatialVersion'])
    request.update(inputId=args.input_id,parameters=deepcopy(result['parameters']),
                   partitionParameters={},_modelIdentity=model_identity(),_metricsOnly=True)
    request['_fixedInitialization']=deepcopy(result['initialization'])
    runs={};states={}
    cases=[(start,.5) for start in args.starts]+[(args.starts[0],.1),(args.starts[0],.9)]
    for start,fraction in cases:
        name=start+'-'+str(fraction)
        options={**request,'start':pd.Timestamp(start).isoformat(),'initialHydrology':{'soilFraction':fraction}}
        anchor_time=result['initialization']['time']
        at_anchor=run(db,{**options,'end':anchor_time},prepared=source)
        states[name]=snapshot(at_anchor)
        output=run(db,options,prepared=source)
        runs[name]=output
        write_new(destination/(name+'-daily.json'),output['daily'])
        if fraction==.5:
            write_new(destination/(name+'-result.json'),output)
        write_new(destination/(name+'-anchor-state.json'),states[name])
        print('完成',name,flush=True)
    ref=args.starts[0]+'-0.5';baseline=runs[ref]
    comparisons={name:comparison(baseline,value) for name,value in runs.items() if name!=ref}
    summary={'inputId':args.input_id,'inputDataSHA256':manifest['dataSHA256'],
             'sourceTask':args.base_task,'sourceRequestSHA256':sha_file(base/'request.json'),
             'sourceResultSHA256':sha_file(base/'result.json'),'modelImplementation':model_identity(),
             'parameterResultSHA256':sha_file(Path(args.parameter_result)) if args.parameter_result else None,
             'spatialVersion':request['spatialVersion'],'parametersSHA256':digest(request['parameters']),
             'initialization':result['initialization'],'baselineCase':ref,'comparisons':comparisons,
             'anchorStates':{k:v['areaWeighted'] for k,v in states.items()},
             'budget':budget(baseline),'outputDirectory':str(destination),
             'note':'首完整湖位日2021-04-24；流量差按参考过程平均流量归一RMSE。'}
    write_new(destination/'summary.json',jsonable(summary))
    print(json.dumps({'outputDirectory':str(destination),'comparisons':comparisons},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
