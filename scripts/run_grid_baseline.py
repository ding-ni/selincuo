"""Create a reviewable research baseline in the clean business preview."""
import sys,time,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psutil
from backend.database import Database
from backend.jobs import Jobs
from backend.research_schemes import template,save
from backend.common import write_json

db=Database(Path(__file__).resolve().parents[1]/'build/grid-revision-20261026/workspace')
value=template(db);value['name']='逐格网连续方案 · 原标准参数基线'
scheme=save(db,value);jobs=Jobs(db)
task=jobs.submit('simulation',{'researchSchemeId':scheme['id'],'name':'逐格网原参数连续基线（2023—2025）'})
start=time.monotonic();peak=0.;process=psutil.Process();last=None
while True:
    task=db.task(task['id']);peak=max(peak,process.memory_info().rss/1024**2)
    message=(task['status'],int(task['progress']//10))
    if message!=last:print(task['status'],round(task['progress'],1),task['message'],flush=True);last=message
    if task['status'] in ('succeeded','failed','cancelled'):break
    time.sleep(1)
evidence={'taskId':task['id'],'schemeId':scheme['id'],'seconds':time.monotonic()-start,'peakSampledRssMiB':peak,'status':task['status'],'message':task['message']}
if task.get('result'):evidence.update({k:task['result'].get(k) for k in ('metrics','diagnostics','initialization','inflowComplete','hours','spatialVersion')})
write_json(db.workspace.parent/'baseline-evidence.json',evidence);print(json.dumps(evidence,ensure_ascii=False),flush=True);jobs.close()
if task['status']!='succeeded':sys.exit(1)
