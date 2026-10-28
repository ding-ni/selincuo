"""Revision-aware monitoring reads sharing the numerical aggregation rules."""
from collections import OrderedDict
from datetime import date,timedelta
from threading import RLock
import numpy as np
import pandas as pd
from .observations import daily_series
_lock=RLock()
_cache=OrderedDict()
_flights={}

def revision(db):
    with db.connect() as c:
        # Importers publish a new source/observation and log changes in the same
        # transaction. MAX on the primary keys is index-backed; COUNT(*) on the
        # entire observations table at every cache hit was needlessly expensive.
        audit=c.execute("SELECT id FROM audit WHERE action LIKE 'observations.%' OR action LIKE 'coverage.%' OR action LIKE 'stations.%' ORDER BY id DESC LIMIT 1").fetchone()
        return (str(db.workspace),c.execute('SELECT MAX(id) FROM observations').fetchone()[0],
                c.execute('SELECT MAX(rowid) FROM sources').fetchone()[0],
                audit[0] if audit else None)

def cached(db,name,make):
    key=(revision(db),name)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
        guard=_flights.setdefault(key,RLock())
    with guard:
        with _lock:
            if key in _cache:return _cache[key]
        try:
            value=make()
            with _lock:
                _cache[key]=value
                while len(_cache)>48:_cache.popitem(last=False)
            return value
        finally:
            with _lock:_flights.pop(key,None)

def selected_sql(station=None,variable=None,step=None,start=None,end=None,lean=False):
    clauses=['s.rowid IN (SELECT MAX(rowid) FROM sources GROUP BY path)'];args=[]
    # A date-only end includes the entire business day, including fractional
    # seconds. Explicit timestamps keep their existing inclusive semantics.
    end_is_day=bool(end and len(end)==10)
    day_after=(date.fromisoformat(end)+timedelta(days=1)).isoformat() if end_is_day else None
    for field,op,value in [('station_id','=',station),('variable','=',variable),('step','=',step),('time','>=',start),('time','<' if end_is_day else '<=',day_after if end_is_day else end)]:
        if value:clauses.append('o.'+field+op+'?');args.append(value)
    fields="o.id,o.station_id,o.variable,o.time,o.step,o.quality,o.value" if lean else "o.*"
    return "WITH ranked AS (SELECT "+fields+",ROW_NUMBER() OVER(PARTITION BY station_id,variable,step,time ORDER BY s.imported_at DESC,o.id DESC) rn FROM observations o JOIN sources s ON s.id=o.source_id WHERE "+' AND '.join(clauses)+') SELECT * FROM ranked WHERE rn=1',args

def catalog(db):
    def make():
        sql,args=selected_sql(lean=True)
        rows=db.rows("SELECT station_id,variable,step,COUNT(*) records,COUNT(CASE WHEN quality IN ('valid','qualified') THEN value END) valid,MIN(time) start,MAX(time) end,MIN(CASE WHEN quality IN ('valid','qualified') AND value IS NOT NULL THEN time END) validStart,MAX(CASE WHEN quality IN ('valid','qualified') AND value IS NOT NULL THEN time END) validEnd FROM ("+sql+") GROUP BY station_id,variable,step",args)
        stations=db.rows('SELECT * FROM stations ORDER BY id')
        for s in stations:
            s['located']=s['lon'] is not None and s['lat'] is not None;s['variables']=[]
            for variable in sorted({r['variable'] for r in rows if r['station_id']==s['id']}):
                parts=[r for r in rows if r['station_id']==s['id'] and r['variable']==variable];valid=[r for r in parts if r['validStart']]
                s['variables'].append({'id':variable,'steps':[r['step'] for r in parts],'records':sum(r['records'] for r in parts),'valid':sum(r['valid'] for r in parts),'start':min((r['validStart'] for r in valid),default=min(r['start'] for r in parts)),'end':max((r['validEnd'] for r in valid),default=max(r['end'] for r in parts)),'recordStart':min(r['start'] for r in parts),'recordEnd':max(r['end'] for r in parts),'validStart':min((r['validStart'] for r in valid),default=None),'validEnd':max((r['validEnd'] for r in valid),default=None)})
        return {'stations':stations}
    return cached(db,'catalog',make)

def page(db,q):
    page=max(1,int(q.get('page',1)));size=min(200,max(1,int(q.get('pageSize',50))))
    sql,args=selected_sql(q.get('station','SLC'),q.get('variable','level'),q.get('step'),q.get('start'),q.get('end'))
    with db.connect() as c:
        total=c.execute('SELECT COUNT(*) FROM ('+sql+')',args).fetchone()[0]
        rows=[dict(r) for r in c.execute(sql+' ORDER BY time,id LIMIT ? OFFSET ?',args+[size,(page-1)*size])]
    return {'rows':rows,'total':total,'page':page,'pageSize':size}

class SeriesView:
    def __init__(self,db):self.rows=db.rows
    def observations(self,station,variable,step=None,start=None,end=None,limit=None):
        sql,args=selected_sql(station,variable,step,start,end,lean=True)
        return self.rows(sql+' ORDER BY time,id',args)

def series(db,station,variable,start=None,end=None):
    full=cached(db,('daily',station,variable),lambda:daily_series(SeriesView(db),station,variable))
    if full.empty:return {'rows':[]}
    lo=pd.Timestamp(start or full.index.min()).floor('D');hi=pd.Timestamp(end or full.index.max()).floor('D')
    if hi<lo or (hi-lo).days>36600:raise ValueError('查询日期范围无效')
    values=full.reindex(pd.date_range(lo,hi,freq='D'))
    return {'rows':[{'time':t.isoformat(),'value':float(v) if np.isfinite(v) else None} for t,v in values.items()]}
