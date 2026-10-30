"""保存正在执行的正式搜索后接续，同一任务与配置不变。"""
import json
from pathlib import Path
import sys
import time
from urllib.request import Request,urlopen

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.common import ROOT,WORKSPACE,read_json,write_json,digest,sha_file


def api(path,body=None):
    req=Request('http://127.0.0.1:8882/api/model/'+path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type':'application/json'})
    with urlopen(req,timeout=60) as stream:return json.load(stream)


identifier='JOB-6536f36e07cc46868255'
target=ROOT/'docs/evidence/完整率定停止接续-20261030.json'
if target.exists():raise SystemExit('本项验收已有记录，不重复停止')
folder=WORKSPACE/'tasks'/identifier
original=read_json(folder/'request.json')
before=api('tasks/'+identifier)
if before['status']!='running':raise SystemExit('正式任务当前不在计算，不执行停止')
api('tasks/'+identifier+'/stop',{})
for attempt in range(60):
    stopped=api('tasks/'+identifier)
    if stopped['status'] not in ('running','stopping'):break
    time.sleep(.5)
if stopped['status']!='cancelled':raise RuntimeError('任务未到保存后的停止状态')
state=read_json(folder/'pipeline-state.json')
optimizer=folder/state['fit']/'optimizer-state.json'
saved=read_json(optimizer)
saved_sha=sha_file(optimizer)
resumed=api('tasks/'+identifier+'/resume',{})
after=read_json(folder/'request.json')
proof={'taskId':identifier,'before':{k:before[k] for k in ('status','progress','message')},
       'stoppedStatus':stopped['status'],'resumedStatus':resumed['status'],
       'configurationSHA256':original['calibrationConfigurationSHA256'],
       'requestUnchanged':digest(original)==digest(after),'phase':state,
       'savedEvaluations':saved['evaluations'],'savedOptimizerSHA256':saved_sha,
       'optimizerUnchangedWhileQueued':saved_sha==sha_file(optimizer),
       'scope':'实际任务停止和接续；继续使用原轨迹、种群、随机状态和冻结资料'}
write_json(target,proof)
print(json.dumps(proof,ensure_ascii=False))
