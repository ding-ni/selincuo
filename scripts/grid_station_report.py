# -*- coding: utf-8 -*-
import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
from backend.database import Database
from backend.common import ROOT,write_json,read_json
from backend.observation_profiles import datum_candidates,quality_series
from backend.simulation import metrics
out=ROOT/'build/grid-revision-20261026';db=Database(out/'workspace');candidate=datum_candidates(db)
frame=pd.DataFrame({'SLC':quality_series(db,'SLC')});scores=[]
for c in candidate['candidates']:
    station=c['station'];frame[station]=quality_series(db,station)
    if c['status']=='statistical-candidate':frame[station+'-candidate']=frame[station]+c['correctionM']
    for year in (2025,2026):
        g=frame.loc[frame.index.year==year];score=metrics(g.SLC,g[station])
        corrected=metrics(g.SLC,g[station+'-candidate']) if station+'-candidate' in g else None
        scores.append({'station':station,'year':year,'purpose':'同年拟合对照，非独立验证' if year==2025 else '偏移稳定性核查','rawVsWorkingMain':score,'statisticalCandidateVsWorkingMain':corrected})
frame.to_csv(out/'shore-original-and-candidates.csv',index_label='date',encoding='utf-8-sig')
write_json(out/'station-evidence.json',{'candidate':candidate,'scores':scores,'mainIsWorkingReferenceOnly':True,'surveyCorrectionApplied':False})
write_json(out/'datum-candidates-current.json',candidate)
print(json.dumps({'id':candidate['id'],'stations':[{'id':c['station'],'status':c['status'],'offsetM':c['offsetM'],'anomalyPoints':c['withinDayDiagnostics']['flaggedPoints'],'anomalySegments':len(c['withinDayDiagnostics']['segments'])} for c in candidate['candidates']]},ensure_ascii=False),flush=True)
