# -*- coding: utf-8 -*-
import sys,time,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psutil
from backend.database import Database
from backend.common import ROOT,write_json,read_json
from backend.observation_profiles import series,_cache
from backend.observation_query import selected_sql
from backend.result_query import read
folder=ROOT/'build/grid-revision-20261026';db=Database(folder/'acceptance-workspace');rows=[]
for station in ('SLC','SLC-N','SLC-E','SLC-S','SLC-W'):
    _cache.clear();t=time.perf_counter();data=series(db,station,'level');first=time.perf_counter()-t
    t=time.perf_counter();series(db,station,'level');cached=time.perf_counter()-t
    rows.append({'station':station,'firstAggregationSeconds':first,'cachedSeconds':cached,'days':len(data['rows'])})
business=Database(folder/'workspace');identifier=read_json(folder/'baseline-evidence.json')['taskId'];queries=[]
for query,frame in [({'time':'2025-12-31T00:00:00'},True),({'time':'2025-12-30T00:00:00'},True),({'unit':0,'start':'2024-01-01','end':'2025-12-31'},False)]:
    t=time.perf_counter();result=read(business,{'resultId':identifier,'variable':'WU',**query},frame=frame);queries.append({'query':query,'seconds':time.perf_counter()-t,'rows':len(result.get('values',result.get('rows',[])))})
sql,args=selected_sql('SLC','level',lean=True)
evidence={'memoryGiB':psutil.virtual_memory().total/1024**3,'logicalCpus':psutil.cpu_count(),'observationCount':db.rows('SELECT COUNT(*) n FROM observations')[0]['n'],'coldDefinition':'in-process daily cache empty; no artificial OS cache flush','queryPlan':db.rows('EXPLAIN QUERY PLAN '+sql+' ORDER BY time,id',args),'monitoring':rows,'stateQueries':queries,'rssMiB':psutil.Process().memory_info().rss/1024**2}
write_json(folder/'performance-evidence.json',evidence);print(json.dumps(evidence,ensure_ascii=False),flush=True)
