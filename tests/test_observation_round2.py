"""Regressions for monitoring quality, pairing, caching and invalidation.

The isolated workspaces are intentionally retained under build; tests never
delete project data or remove temporary directories.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch
import threading
import time
import uuid
import sqlite3

import pandas as pd

from backend.common import ROOT
from backend.data_exchange import import_records
from backend.database import Database
from backend import observation_profiles as profiles
from backend import observation_query as query
from backend.observations import confirm_coverage
from backend.stations import datum_at


def sample(time_value, value, quality='valid', index=0):
    return {'id':index,'time':time_value,'value':value,'quality':quality,'step':'irregular'}


class MonitoringCorrections(TestCase):
    def test_date_only_filter_includes_all_seconds_and_plain_dates(self):
        with sqlite3.connect(':memory:') as conn:
            conn.execute('CREATE TABLE sources(id TEXT,path TEXT,imported_at TEXT)')
            conn.execute('CREATE TABLE observations(id INTEGER,source_id TEXT,station_id TEXT,variable TEXT,time TEXT,step TEXT,quality TEXT,value REAL)')
            conn.execute("INSERT INTO sources VALUES('a','source','2025')")
            conn.executemany("INSERT INTO observations VALUES(?,?,?,?,?,?,?,?)",[
                (1,'a','SLC','level','2025-06-01','day','valid',1.),
                (2,'a','SLC','level','2025-06-01T23:59:59.999','irregular','valid',2.),
                (3,'a','SLC','level','2025-06-02T00:00:00','irregular','valid',3.),
            ])
            sql,args=query.selected_sql('SLC','level',start='2025-06-01',end='2025-06-01',lean=True)
            self.assertEqual([r[0] for r in conn.execute(sql+' ORDER BY time',args)],[1,2])

    def test_surveyed_datum_is_resolved_by_station_and_complete_day(self):
        registry={'referenceStation':'SLC','periods':[
            {'stationId':'SLC','status':'working-reference','recordType':'water-level-record','unit':'m'},
            {'stationId':'SLC-N','status':'survey-verified','surveySource':'联测记录 A',
             'referenceStation':'SLC','effectiveFrom':'2025-06-01T00:00:00','effectiveTo':'2025-06-03T12:00:00',
             'recordType':'water-level-record','unit':'m','zeroElevationM':4500.,'referenceOffsetM':-.2},
            {'stationId':'SLC-E','status':'survey-verified','surveySource':'联测记录 B',
             'referenceStation':'SLC','effectiveFrom':'2025-06-01T00:00:00','effectiveTo':None,
             'recordType':'water-gauge-reading','unit':'m','zeroElevationM':4500.},
            {'stationId':'SLC-S','status':'unverified','recordType':'water-level-record','unit':'m',
             'zeroElevationM':4500.,'referenceOffsetM':0.,'referenceStation':'SLC'},
        ]}
        rows=[{'time':'2025-05-31T00:00:00','value':4543.2},
              {'time':'2025-06-01T00:00:00','value':4543.3},
              {'time':'2025-06-03T00:00:00','value':4543.4}]
        with patch.object(profiles,'daily_quality',return_value=rows),patch.object(profiles,'read_json',return_value=registry), \
             patch.object(profiles,'revision',return_value=('unique-'+uuid.uuid4().hex,)):
            result=profiles.series(None,'SLC-N','level')
        self.assertEqual([r['value'] for r in result['rows']],[4543.2,4543.3,4543.4])
        self.assertIsNone(result['rows'][0]['referenceValue'])
        self.assertAlmostEqual(result['rows'][1]['referenceValue'],4543.1)
        self.assertIsNone(result['rows'][2]['referenceValue'])
        self.assertEqual(result['rows'][1]['referenceDatum']['surveySource'],'联测记录 A')
        self.assertIsNone(datum_at('SLC-S','2025-06-01',registry)['referenceOffsetM'])
        self.assertEqual(datum_at('SLC-E','2025-06-01',registry)['referenceOffsetM'],4500.)
        missing_offset={**registry,'periods':[{k:v for k,v in registry['periods'][1].items() if k!='referenceOffsetM'}]}
        self.assertFalse(datum_at('SLC-N','2025-06-01',missing_offset)['absoluteConversionAvailable'])

    def test_reference_cache_tracks_datum_registration(self):
        period={'stationId':'SLC-N','status':'survey-verified','surveySource':'联测记录',
                'referenceStation':'SLC','effectiveFrom':'2025-01-01','recordType':'water-level-record',
                'unit':'m','referenceOffsetM':-.2}
        registry={'referenceStation':'SLC','periods':[period]}
        with patch.object(profiles,'revision',return_value=('unique-'+uuid.uuid4().hex,)), \
             patch.object(profiles,'daily_quality',return_value=[{'time':'2025-06-01','value':100.}]) as daily, \
             patch.object(profiles,'read_json',side_effect=lambda *args:registry):
            self.assertAlmostEqual(profiles.series(None,'SLC-N','level')['rows'][0]['referenceValue'],99.8)
            self.assertAlmostEqual(profiles.series(None,'SLC-N','level')['rows'][0]['referenceValue'],99.8)
            self.assertEqual(daily.call_count,1)
            period['referenceOffsetM']=-.3
            self.assertAlmostEqual(profiles.series(None,'SLC-N','level')['rows'][0]['referenceValue'],99.7)
            self.assertEqual(daily.call_count,2)

    def test_no_nearby_pair_is_empty_and_qualified_values_can_pair(self):
        main=[sample('2025-05-01T12:00:00',4544.)]
        shore=[sample('2025-05-01T13:00:00',4544.2,'qualified')]
        with patch.object(profiles,'SeriesView') as view:
            view.return_value.observations.side_effect=lambda station,variable: main if station=='SLC' else shore
            result=profiles._paired_points(None,'SLC-S')
            self.assertTrue(result.empty)
            self.assertEqual(list(result.columns),['time','difference'])
            shore[0]['time']='2025-05-01T12:10:00'
            result=profiles._paired_points(None,'SLC-S')
            self.assertEqual(len(result),1)
            self.assertAlmostEqual(result.iloc[0]['difference'],.2)

    def test_south_is_decided_by_data_not_station_name(self):
        times=pd.date_range('2025-04-01',periods=92,freq='D')
        paired=pd.DataFrame({'time':times,'difference':[.1]*len(times)})
        database=type('DB',(),{'rows':lambda self,*args:[]})()
        with patch.object(profiles,'revision',return_value=('unique-'+uuid.uuid4().hex,)), \
             patch.object(profiles,'_point_samples',return_value=[(1,1.)]), \
             patch.object(profiles,'_paired_points',return_value=paired):
            candidates=profiles.datum_candidates(database)['candidates']
        south=next(c for c in candidates if c['station']=='SLC-S')
        self.assertEqual(south['status'],'statistical-candidate')
        self.assertEqual(south['reasonCode'],'STATISTICAL_CANDIDATE')
        self.assertAlmostEqual(south['correctionM'],-.1)
        self.assertEqual(south['estimationPeriod']['endExclusive'],'2026-01-01')

    def test_unavailable_candidate_preserves_observation_and_calls_analysis_once(self):
        original=[{'time':'2025-06-01T00:00:00','value':4543.2,'quality':'coverage-valid'}]
        with patch.object(profiles,'daily_quality',return_value=original), \
             patch.object(profiles,'revision',return_value=('unique-'+uuid.uuid4().hex,)), \
             patch.object(profiles,'datum_candidates',return_value={'id':'A','candidates':[{'station':'SLC-S','status':'time-varying-under-review','reason':'差值随时段变化','correctionM':None}]}) as compute:
            result=profiles.series(None,'SLC-S','level',profile='candidate')
        self.assertEqual(compute.call_count,1)
        self.assertEqual(result['rows'][0]['value'],4543.2)
        self.assertIsNone(result['rows'][0]['adjustedValue'])
        self.assertIn('差值随时段变化',result['message'])

    def test_cold_same_key_daily_computation_is_single_flight(self):
        barrier=threading.Barrier(4)
        rows=[sample('2025-06-01T00:00:00',1.),sample('2025-06-02T00:00:00',2.,index=1)]
        def read(*args,**kwargs):
            time.sleep(.06)
            return rows
        def run(_):
            barrier.wait(timeout=5)
            return profiles.daily_quality(None,'SLC-S')
        with patch.object(profiles,'revision',return_value=('unique-'+uuid.uuid4().hex,)), \
             patch.object(profiles,'SeriesView') as view:
            view.return_value.observations.side_effect=read
            with ThreadPoolExecutor(max_workers=4) as pool:
                outputs=list(pool.map(run,range(4)))
            self.assertEqual(view.return_value.observations.call_count,1)
            self.assertTrue(all(result==outputs[0] for result in outputs))

    def test_incremental_import_and_coverage_changes_invalidate_views(self):
        workspace=ROOT/'build/第二轮精修-20261028/观测'/('隔离库-'+uuid.uuid4().hex[:12])
        db=Database(workspace)
        with db.connect() as connection:
            connection.execute("INSERT INTO stations VALUES('A','站点','lake',NULL,NULL,'{}')")
        document={'schemaVersion':1,'projectId':'selinco','sourceKey':'unit','records':[
            {'station':'A','variable':'level','time':'2025-01-01T00:00:00','value':4544,'unit':'m','step':'day'}]}
        first=import_records(db,document)
        self.assertEqual(profiles.series(db,'A','level')['rows'][0]['value'],4544)
        previous=query.revision(db)
        document['records'][0]['value']=4545
        second=import_records(db,document)
        self.assertNotEqual(query.revision(db),previous)
        self.assertEqual(profiles.series(db,'A','level')['rows'][0]['value'],4545)
        self.assertNotEqual(first['sourceId'],second['sourceId'])
        previous=query.revision(db)
        confirm_coverage(db,{'sourceId':second['sourceId'],'station':'A','variable':'level','start':'2025-01-01','end':'2025-01-02'})
        self.assertNotEqual(query.revision(db),previous)


if __name__ == '__main__':
    main()
