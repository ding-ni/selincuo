import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
from backend.database import Database
from backend.observation_query import catalog,page,series
from backend.result_query import metadata,read,source,final
from backend.common import ROOT,read_json
from backend.calibration_workflow import run

class BusinessQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Database(self.temp.name)
        with self.db.connect() as c:
            c.execute("INSERT INTO stations VALUES('A','测试雨量站','rain',NULL,NULL,'{}')")
            c.execute("INSERT INTO sources VALUES('S','data.xlsx','sha',0,'2026-10-25','{}')")
    def tearDown(self):
        self.temp.cleanup()
    def add(self,rows):
        with self.db.connect() as c:
            c.executemany('INSERT INTO observations(source_id,station_id,variable,time,step,unit,value,raw_text,quality,sheet,cell) VALUES(?,?,?,?,?,?,?,?,?,?,?)',rows)
    def row(self,t,v,cell,step='day',source='S'):
        return (source,'A','precipitation',t,step,'mm',v,str(v),'valid','data',cell)
    def test_actual_variables_gaps_and_revision_invalidation(self):
        self.add([self.row('2024-01-01T00:00:00',1,'A1'),self.row('2024-01-03T00:00:00',3,'A3')])
        cat=catalog(self.db)['stations'][0]
        self.assertEqual([v['id'] for v in cat['variables']],['precipitation'])
        self.assertFalse(cat['located'])
        first=series(self.db,'A','precipitation')['rows']
        self.assertEqual([r['value'] for r in first],[1,None,3])
        with self.db.connect() as c:
            c.execute("INSERT INTO sources VALUES('S2','data.xlsx','sha2',0,'2026-10-26','{}')")
        self.add([self.row('2024-01-01T00:00:00',7,'A1',source='S2')])
        self.assertEqual(series(self.db,'A','precipitation')['rows'][0]['value'],7)
        self.assertEqual(catalog(self.db)['stations'][0]['variables'][0]['valid'],1)
        self.assertEqual(page(self.db,{'station':'A','variable':'precipitation'})['total'],1)
    def test_pages_reach_past_3000_and_are_stable(self):
        dates=pd.date_range('2024-01-01',periods=6001,freq='h')
        self.add([self.row(t.isoformat(),i,'C'+str(i),'irregular') for i,t in enumerate(dates)])
        result=page(self.db,{'station':'A','variable':'precipitation','page':61,'pageSize':50})
        self.assertEqual(result['total'],6001)
        self.assertEqual(result['rows'][0]['value'],3000)
        self.assertEqual(result,page(self.db,{'station':'A','variable':'precipitation','page':61,'pageSize':50}))
    def test_aggregation_has_no_100000_record_cutoff(self):
        dates=pd.date_range('2024-01-01T01:00',periods=100008,freq='h')
        self.add([self.row(t.isoformat(),1,'H'+str(i),'hour') for i,t in enumerate(dates)])
        result=series(self.db,'A','precipitation')['rows']
        self.assertEqual(sum(r['value'] or 0 for r in result),100008)
        self.assertEqual(result[-1]['value'],24)

class FinalStateQueryTests(unittest.TestCase):
    def test_exact_final_source_day_end_and_flux_aggregation(self):
        with tempfile.TemporaryDirectory() as temp:
            db=Database(temp);identifier=read_json(ROOT/'data/final-results/catalog.json')['results'][0]['id']
            meta=metadata(db,identifier)
            self.assertIn('2024-04-22',meta['lakeInitializationDays'])
            self.assertIn('2025-04-09',meta['lakeInitializationDays'])
            self.assertEqual(len(meta['units']),30)
            self.assertEqual([v['id'] for v in meta['variables'][:8]],['swe','snowLiquid','WU','WL','WD','freeWater','FR','glacierStorage'])
            self.assertFalse(meta['canContinue'])
            task,data,state=source(db,identifier)
            np.testing.assert_array_equal(data['unitStates'][-1],state['unitStates'])
            frame=read(db,{'resultId':identifier,'variable':'WU','time':meta['end']},frame=True)
            np.testing.assert_array_equal([v['value'] for v in frame['values']],data['unitStates'][-1,:,2])
            self.assertFalse(frame['completeCheckpoint'])
            q={'resultId':identifier,'variable':'swe','unit':0,'start':'2024-01-01','end':'2024-01-03'}
            rows=read(db,q)['rows'];times=pd.to_datetime(data['time'])
            for row in rows:
                idx=times.get_loc(pd.Timestamp(row['time'])+pd.Timedelta(days=1))
                self.assertEqual(row['value'],data['unitStates'][idx,0,0])
            flux=read(db,{**q,'variable':'snowmelt'})['rows']
            for row in flux:
                day=pd.Timestamp(row['time']);mask=(times>day)&(times<=day+pd.Timedelta(days=1))
                self.assertAlmostEqual(row['value'],data['unitDiagnostics'][mask,0,4].sum())
            self.assertTrue(final(db,identifier)['completeCheckpoint'])
            self.assertFalse(final(db,identifier)['compatible'])

class ContinuationScoringTests(unittest.TestCase):
    def test_carried_lake_level_is_not_an_initialization_observation(self):
        spatial=read_json(ROOT/'data/derived/spatial/manifest.json')
        task={'kind':'continuation','result':{'units':spatial['units'],'spatialVersion':spatial['version'],'diagnostics':{'lakeWindows':[{'start':'2025-04-09T08:00:00'}]}}}
        data={'time':pd.to_datetime(['2025-06-01T01:00','2025-06-01T02:00']).asi8,'lakeLevel':np.array([4544.,4544.1])}
        state={'identity':{'modelImplementation':'current','spatialVersion':spatial['version']}}
        def entry(db,identifier):
            # Each logical result has its own cold metadata entry, preserving
            # the original continuation-versus-window scoring assertions.
            return {'task':task,'data':data,'state':state,
                    'times':pd.DatetimeIndex(data['time'].astype('datetime64[ns]')),
                    'initializationDays':None,'initializationDaysBuilt':False}
        with patch('backend.result_query._entry',side_effect=entry),patch('backend.result_query.model_identity',return_value='current'):
            self.assertEqual(metadata(None,'continued')['lakeInitializationDays'],['2025-04-09'])
            task['result'].pop('diagnostics')
            self.assertIsNone(metadata(None,'legacy')['lakeInitializationDays'])

class CalibrationWorkflowTests(unittest.TestCase):
    def test_stage_handoff_progress_and_single_final_checkpoint(self):
        calls=[];progress=[]
        def calibrate(db,request,folder,report,cancel):
            calls.append(json.loads(json.dumps(request)))
            report(50,'计算')
            for name in ('hourly.npz','daily.csv','checkpoint.json'):(folder/name).write_text(request['stage'],encoding='utf-8')
            return {'parameters':{'KC':.7},'partitionParameters':{'duja':{'KC':.8 if request['stage']=='runoff' else .9}},'calibration':{'stage':request['stage']},'checkpoint':{'validAt':'2025-01-01'}}
        with tempfile.TemporaryDirectory() as tmp,patch('backend.calibration_workflow.calibrate',side_effect=calibrate):
            result=run(None,{'target':'duja','parameters':{'KC':.7},'partitionParameters':{'duja':{'KC':.75}}},Path(tmp),lambda p,m:progress.append(p),lambda:False)
            self.assertEqual([c['stage'] for c in calls],['runoff','routing'])
            self.assertEqual(calls[1]['partitionParameters']['duja']['KC'],.8)
            self.assertEqual(result['partitionParameters']['duja']['KC'],.9)
            self.assertEqual((Path(tmp)/'checkpoint.json').read_text(encoding='utf-8'),'routing')
            self.assertEqual(progress,sorted(progress))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(InterruptedError):
                run(None,{'target':'duja'},Path(tmp),lambda p,m:None,lambda:True)

if __name__=='__main__':unittest.main()
