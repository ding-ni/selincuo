"""Replay saved DE proposals without running the hydrological model."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.common import ROOT, read_json, sha_file, write_json
from backend.model_core import parameters


def reconstruct(folder):
    fit=read_json(folder/'fit-result.json');state=read_json(folder/'optimizer-state.json')
    output=folder/'population-replay'
    if output.exists():raise ValueError('种群重建已存在，请保留原批次')
    rows={}
    with (folder/'search-trace.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            row=json.loads(line);rows[row['evaluation']]=row
    trace=[rows[k] for k in sorted(rows)]
    position=0;best=float('inf');working=deepcopy(fit['initialParameters']);skipped=[];files=[]
    def update(row):
        nonlocal best,working
        if row['objective']<best:
            best=row['objective'];working={**working,**row['parameters']}
    while position<len(trace) and trace[position]['phase']=='initial':
        update(trace[position]);position+=1
    for index,stage in enumerate(state['records']):
        keys=stage['keys'];box=np.asarray([fit['bounds'][k] for k in keys],float)
        for _ in range(sum('objective' in value for value in stage['responses'].values())):
            row=trace[position]
            assert row['phase']=='response' and row['stage']==stage['name'] and row['cycle']==stage['cycle']
            update(row);position+=1
        for start in stage['starts']:
            seed=start['seed'];rng=np.random.default_rng(seed);base=deepcopy(working)
            population=[[base[k] for k in keys]];scores=[best];slot=0;generation=0;proposals=0;history=[]
            size=max(12,4*len(keys));last=None
            for _ in range(start['evaluations']):
                while True:
                    proposals+=1
                    matrix=np.asarray(population)
                    target=None
                    if len(matrix)<size:
                        vector=box[:,0]+rng.random(len(keys))*(box[:,1]-box[:,0])
                    else:
                        target=slot;choices=[i for i in range(len(matrix)) if i!=slot]
                        a,b,c=rng.choice(choices,3,replace=False)
                        scale=rng.uniform(.5,.95)
                        mutant=np.clip(matrix[a]+scale*(matrix[b]-matrix[c]),box[:,0],box[:,1])
                        cross=rng.random(len(keys))<.85;cross[rng.integers(len(keys))]=True
                        vector=np.where(cross,mutant,matrix[slot])
                    candidate={**base,**dict(zip(keys,map(float,vector)))}
                    try:parameters(candidate)
                    except ValueError:continue
                    break
                while True:
                    row=trace[position]
                    expected=np.asarray([row['parameters'][k] for k in keys])
                    if np.array_equal(vector,expected):break
                    # One early stop completed the objective before its DE
                    # update. The same proposal was evaluated after resume.
                    if last and row['parameters']==last['parameters'] and row['objective']==last['objective']:
                        skipped.append({'evaluation':row['evaluation'],'reason':'repeated proposal across saved stop'})
                        update(row);position+=1
                        continue
                    raise ValueError('轨迹与随机种群重放不一致：%s，第%d次'%(folder.name,row['evaluation']))
                assert row['phase']=='search' and row['seed']==seed and row['stage']==stage['name']
                score=row['objective'];update(row);position+=1;last=row
                if target is None:
                    population.append(vector.tolist());scores.append(score)
                else:
                    if score<scores[target]:population[target]=vector.tolist();scores[target]=score
                    slot=(slot+1)%size
                    if slot==0:generation+=1;history.append(min(scores))
            assert generation==start['generations']
            assert min(scores)==start['bestObjective']
            name='population-%02d-%d.json'%(index+1,seed)
            value={'stage':stage['name'],'cycle':stage['cycle'],'seed':seed,'keys':keys,
                'baseParameters':base,'population':population,'scores':scores,'rngState':rng.bit_generator.state,
                'slot':slot,'generations':generation,'proposals':proposals,'used':start['evaluations'],
                'converged':start['converged'],'termination':start['termination'],
                'sourceTraceSHA256':fit['traceSHA256'],'reconstructedWithoutModelEvaluations':True}
            write_json(output/name,value)
            files.append({'file':name,'sha256':sha_file(output/name)})
    assert position==len(trace),(position,len(trace))
    assert best==fit['bestObjective']
    report={'fit':fit['fit'],'identity':fit['identity'],'traceSHA256':fit['traceSHA256'],
        'populations':files,'repeatedEvaluations':skipped,'modelEvaluationsPerformed':0,
        'method':'deterministic replay of saved candidates, objective values, physical checks and seeded DE random proposals'}
    write_json(output/'manifest.json',report)
    return {'fit':fit['fit'],'populations':len(files),'repeatedEvaluations':skipped}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--task',default='JOB-4934a5d35c9344a59353')
    args=parser.parse_args()
    from backend.common import WORKSPACE
    base=WORKSPACE/'tasks'/args.task
    print(json.dumps([reconstruct(base/name) for name in ('forward','reverse','all-data')],ensure_ascii=True))


if __name__=='__main__':main()
