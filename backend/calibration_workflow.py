"""One job owns sequential calibration stages and their cancellation."""
from copy import deepcopy
import shutil
from .operational_scheme import catalog,definition
from .calibration import calibrate
from .common import write_json

def prepare(db,payload):
    scheme=definition();choice=catalog(db)[0]
    if not choice['available']:raise ValueError('缺少标准方案气象资料')
    target=payload.get('target','duja')
    if target not in ('duja','lake'):raise ValueError('请选择杜加产汇流或湖泊蒸发率定')
    return {**payload,'target':target,'inputId':choice['inputId'],'start':scheme['initialization']['origin'],'end':choice['end'],'parameters':deepcopy(scheme['parameters']),'partitionParameters':deepcopy(scheme['partitionParameters']),'routingMode':scheme['routingMode'],'objectiveProfile':'balanced','calibrationYear':2024}

def run(db,request,folder,progress,cancelled):
    stages=['runoff','routing'] if request['target']=='duja' else ['lake'];working=deepcopy(request);records=[]
    for i,stage in enumerate(stages):
        if cancelled():raise InterruptedError('用户停止率定')
        child=folder/stage;child.mkdir(exist_ok=True)
        working.update(stage=stage,calibrationPartition='duja' if stage!='lake' else None,maxEvaluations=request.get('maxEvaluations',120 if stage!='lake' else 30))
        result=calibrate(db,working,child,lambda p,m:progress((i*100+p)/len(stages),m),cancelled)
        records.append({'stage':stage,'calibration':result['calibration']})
        working['parameters']=deepcopy(result['parameters']);working['partitionParameters']=deepcopy(result.get('partitionParameters',{}))
    for name in ('hourly.npz','daily.csv','checkpoint.json'):shutil.copy2(child/name,folder/name)
    result['calibrationWorkflow']={'target':request['target'],'stages':records}
    write_json(folder/'result.json',result)
    return result
