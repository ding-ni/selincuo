"""An explicitly budgeted training-only research candidate via the real task API."""
import json,time,requests
from pathlib import Path
base='http://127.0.0.1:8882/api/model/'
def api(path,value=None):
    r=requests.post(base+path,json=value,timeout=120) if value is not None else requests.get(base+path,timeout=120)
    r.raise_for_status();return r.json()
value=api('research-template');value['name']='逐格网训练期率定 · 12次产流与12次汇流'
value['search']['budgets'].update(runoff=12,routing=12,lake=8)
scheme=api('schemes',{'payload':value});task=api('tasks',{'kind':'calibration','researchSchemeId':scheme['id'],'name':value['name']})
start=time.monotonic();last=None
while True:
    task=api('tasks/'+task['id']);status=(task['status'],int(task['progress']//5))
    if status!=last:print(task['status'],round(task['progress'],1),task['message'],flush=True);last=status
    if task['status'] in ('failed','cancelled','succeeded'):break
    time.sleep(2)
folder=Path(__file__).resolve().parents[1]/'build/grid-revision-20261026'
evidence={'taskId':task['id'],'schemeId':scheme['id'],'status':task['status'],'message':task['message'],'seconds':time.monotonic()-start}
if task.get('result'):evidence.update({k:task['result'].get(k) for k in ('metrics','calibration','diagnostics','parameters','partitionParameters')})
(folder/'calibration-evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in evidence.items() if k not in ('calibration','parameters','partitionParameters')},ensure_ascii=False),flush=True)
if task['status']!='succeeded':raise SystemExit(1)
