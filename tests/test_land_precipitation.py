import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from backend.common import canonical, digest, now
from backend.database import Database
from backend.fast_calibration import DujaEvaluator
from backend.grid_simulation import prefix_identity, run, unit_forcing
from backend.land_precipitation import factors_for_points, validate_parameters
from backend.model_core import DEFAULT_PARAMETERS
from backend.observation_profiles import daily_quality
from backend.observations import daily_series
from scripts.站点暖雨订正对照 import (
    EVENT_TEMPERATURE_PAIRING, event_temperature_endpoint, make_profile, station_samples,
)


def profile():
    return {'method':'station-warm-rain-spatial-v1', 'scope':'land-warm-rain',
            'parentDataSHA256':'forcing', 'temperatureThresholdC':5.0,
            'memberLapseRateCPerM':-0.0065, 'groupLapseRateBoundsCPerM':[-0.01,-0.002],
            'modelRainTemperatureMaxC':5.0, 'radiusKm':100.0, 'distanceRegularizationKm':5.0,
            'stations':{'SLC':{'lon':89.,'lat':32.,'seasonFactors':{'JJA':0.5}}}}


class WarmRainTests(unittest.TestCase):
    def setUp(self):
        self.profile=profile()
        self.times=pd.date_range('2025-07-01',periods=97,freq='h')
        self.spatial={'version':'AGG-test','unitStrategy':'partition-elevation-surface-area-weighted-v1',
          'forcingGridVersion':'g','grids':[{'id':'a','lon':89.,'lat':32.,'referenceElevation':5000.},
            {'id':'b','lon':89.01,'lat':32.,'referenceElevation':5000.}],
          'units':[{'id':'unit','partition':'duja','kind':'land','areaKm2':10.,'basin':'zajia'}],
          'aggregation':[{'unitId':'unit','members':[{'gridIndex':0,'areaWeight':.5,'elevationOffsetM':0.},
                                                   {'gridIndex':1,'areaWeight':.5,'elevationOffsetM':0.}]}],
          'lakeWeights':[.5,.5],'lakeAreaKm2':20.,'drainage':{'inflowComplete':False}}
        temp=np.tile([10.,0.],(len(self.times),1));temp[1]=[10.,-2.]
        self.arrays={'precipitation':np.ones_like(temp),'temperature':temp,'evaporation':np.zeros_like(temp)}
        self.source={'spatial':self.spatial,'times':self.times,'arrays':self.arrays,
          'manifest':{'id':'INPUT-test','dataSHA256':'forcing','transformationSHA256':'lake',
                      'landPrecipitationCorrection':self.profile,'landTransformationSHA256':digest(self.profile)}}
        self.request={'inputId':'INPUT-test','spatialVersion':'AGG-test','start':self.times[0].isoformat(),
          'end':self.times[-1].isoformat(),'parameters':deepcopy(DEFAULT_PARAMETERS),
          'initialHydrology':{'soilFraction':.5},'_fixedInitialization':{'time':'2025-07-01T12:00:00',
          'level':4543.,'method':'test'},'_observations':{'profile':'test','level':{},'flow':{}},
          '_modelIdentity':'test','search':{}}

    def test_member_transform_and_group_cold_guard(self):
        p=deepcopy(DEFAULT_PARAMETERS)
        rain,temp,_=unit_forcing(self.spatial,self.arrays,np.arange(3),p,{},self.profile,self.times)
        # Midnight ends the previous day (June), which is still JJA.
        factor=factors_for_points(self.profile,[(89.,32.)])[2,0]
        self.assertAlmostEqual(rain[0,0],.5*(factor+1.))
        self.assertEqual(rain[1,0],1.)  # warm member cannot contaminate a cold aggregate
        self.assertAlmostEqual(temp[0,0],5.)

    def test_same_implementation_in_full_and_fast_with_lake_untouched(self):
        full=run(None,self.request,prepared=self.source)
        fast=DujaEvaluator(self.source,self.request)
        lookup={r['date'][:10]:r['duja'] for r in full['daily']}
        for day,value in zip(fast.days,fast.daily(self.request['parameters'])):
            if np.isfinite(value) and day.date().isoformat() in lookup:
                self.assertAlmostEqual(value,lookup[day.date().isoformat()],places=9)
        baseline=deepcopy(self.source);baseline['manifest'].pop('landPrecipitationCorrection');baseline['manifest'].pop('landTransformationSHA256')
        raw=run(None,self.request,prepared=baseline)
        for left,right in zip(full['daily'],raw['daily']):
            self.assertEqual(left['lakeRainMm'],right['lakeRainMm'])
        self.assertNotEqual(prefix_identity(self.source,self.times[-1]),prefix_identity(baseline,self.times[-1]))

    def test_transformed_history_continues_and_cannot_reuse_other_forcing(self):
        first=run(None,{**self.request,'end':self.times[48].isoformat()},prepared=self.source)
        workspace=Path(tempfile.mkdtemp(prefix='selinco-warm-rain-continuation-'))
        manifest_dir=workspace/'inputs/products/INPUT-test';manifest_dir.mkdir(parents=True)
        (manifest_dir/'manifest.json').write_text(json.dumps(self.source['manifest']),encoding='utf-8')
        db=SimpleNamespace(workspace=workspace,items=lambda _: [{'id':'INPUT-test','manifest':self.source['manifest']}])
        later={**self.request,'start':self.times[49].isoformat(),'_parentState':first['checkpoint']}
        whole=run(None,self.request,prepared=self.source)
        resumed=run(db,later,prepared=self.source)
        np.testing.assert_allclose(whole['checkpoint']['unitStates'],resumed['checkpoint']['unitStates'],atol=1e-12)
        self.assertEqual(whole['checkpoint']['lake'],resumed['checkpoint']['lake'])
        baseline=deepcopy(self.source);baseline['manifest'].pop('landPrecipitationCorrection');baseline['manifest'].pop('landTransformationSHA256')
        with self.assertRaises(ValueError):run(db,later,prepared=baseline)

    def test_full_and_fast_share_the_fixed_warm_precipitation(self):
        from backend.fast_basin_calibration import BasinEvaluator
        request={**self.request,'parameterTransfer':{'mode':'duja-shared'},'partitionParameters':{}}
        evaluator=BasinEvaluator(self.source,request)
        full=run(None,request,prepared=self.source)
        fast=evaluator.daily(request['parameters'])
        fast=fast.set_index(fast.index)
        full_rows=pd.DataFrame(full['daily']);full_rows.index=pd.to_datetime(full_rows.date)
        for col in ('duja','total','lakeLevel'):
            a=fast[col].reindex(full_rows.index);b=full_rows[col]
            mask=a.notna()&b.notna()
            np.testing.assert_allclose(a[mask],b[mask],atol=1e-9)

    def test_range_and_seasons(self):
        validate_parameters(self.profile,self.request['parameters'])
        for key,value in [('lapseRate',-.011),('rainTemperature',5.1)]:
            p=deepcopy(self.request['parameters']);p[key]=value
            with self.assertRaises(ValueError):validate_parameters(self.profile,p)
        factors=factors_for_points(self.profile,[(89.,32.),(95.,32.)])
        self.assertAlmostEqual(factors[2,1],1.)
        self.assertEqual(factors[1,0],1.)
        self.assertLess(factors[2,0],1.)


class RainCoverageTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix='selinco-rain-coverage-'))
        self.db=Database(self.root)
        with self.db.connect() as c:
            c.execute('INSERT OR IGNORE INTO stations VALUES(?,?,?,?,?,?)',('SLC','lake','lake',89.,32.,'{}'))
            for source in ('daily','confirmed','unknown'):
                c.execute('INSERT INTO sources VALUES(?,?,?,?,?,?)',(source,source,source,1,now(),'{}'))
            rows=[('daily','2025-01-01T00:00:00','day',2.,'valid'),
                  ('confirmed','2025-01-01T00:00:00','irregular',.3,'valid'),
                  ('confirmed','2025-01-01T13:00:00','irregular',.7,'valid'),
                  ('confirmed','2025-01-03T14:00:00','irregular',4.,'valid'),
                  ('unknown','2024-01-02T12:00:00','irregular',9.,'valid')]
            for i,(src,time,step,val,quality) in enumerate(rows):
                c.execute('''INSERT INTO observations(source_id,station_id,variable,time,step,unit,value,raw_text,quality,sheet,cell)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(src,'SLC','precipitation',time,step,'mm',val,str(val),quality,'s',str(i)))
            meta={'coverageConfirmed':True,'zeroForAbsent':True,'stoppedIntervals':[],'eventTimeMeaning':'occurrence-time'}
            c.execute('INSERT INTO coverage VALUES(?,?,?,?,?,?,?,?,?)',('confirmed','SLC','precipitation','irregular',
               '2025-01-01T00:00:00','2025-01-03T00:00:00',3,3,canonical(meta)))
            c.execute('INSERT INTO coverage VALUES(?,?,?,?,?,?,?,?,?)',('unknown','SLC','precipitation','irregular',
               '2024-01-02T12:00:00','2024-01-02T12:00:00',1,1,canonical({'coverageConfirmed':False})))

    def test_daily_preferred_zeros_only_within_coverage_and_midnight_current_day(self):
        result=daily_series(self.db,'SLC','precipitation')
        self.assertEqual(result.loc['2025-01-01'],2.)
        self.assertEqual(result.loc['2025-01-02'],0.)
        self.assertNotIn(pd.Timestamp('2025-01-03'),result.index)
        self.assertNotIn(pd.Timestamp('2024-01-02'),result.index)
        with self.db.connect() as c:c.execute("UPDATE observations SET value=NULL,quality='missing' WHERE source_id='daily'")
        result=daily_series(self.db,'SLC','precipitation')
        self.assertAlmostEqual(result.loc['2025-01-01'],1.)

    def test_stopped_interval_excludes_entire_intersecting_day(self):
        meta={'coverageConfirmed':True,'zeroForAbsent':True,
              'stoppedIntervals':[['2025-01-02T12:00:00','2025-01-02T15:00:00']]}
        with self.db.connect() as c:
            c.execute("UPDATE coverage SET metadata=? WHERE source_id='confirmed'",(canonical(meta),))
        result=daily_series(self.db,'SLC','precipitation')
        self.assertEqual(result.loc['2025-01-01'],2.)
        self.assertTrue(pd.isna(result.loc['2025-01-02']))

    def test_quality_axis_includes_confirmed_tail_and_marks_stops_missing(self):
        meta={'coverageConfirmed':True,'zeroForAbsent':True,
              'stoppedIntervals':[['2025-01-02T12:00:00','2025-01-02T15:00:00']]}
        with self.db.connect() as c:
            c.execute("UPDATE coverage SET end='2025-01-06T00:00:00',metadata=? WHERE source_id='confirmed'",(canonical(meta),))
        result=daily_quality(self.db,'SLC','precipitation')
        self.assertEqual([r['time'][:10] for r in result],
                         ['2025-01-01','2025-01-02','2025-01-03','2025-01-04','2025-01-05'])
        self.assertEqual([r['value'] for r in result],[2.,None,4.,0.,0.])
        self.assertEqual(result[1]['quality'],'missing')

    def test_quality_with_confirmed_coverage_and_no_event_rows(self):
        meta={'coverageConfirmed':True,'zeroForAbsent':True,
              'stoppedIntervals':[['2025-01-02T12:00:00','2025-01-04T00:00:00']]}
        with self.db.connect() as c:
            c.execute('INSERT INTO stations VALUES(?,?,?,?,?,?)',('EMPTY','empty','rain',89.,32.,'{}'))
            c.execute('INSERT INTO sources VALUES(?,?,?,?,?,?)',('empty','empty','empty',1,now(),'{}'))
            c.execute('INSERT INTO coverage VALUES(?,?,?,?,?,?,?,?,?)',('empty','EMPTY','precipitation','irregular',
                '2025-01-01T00:00:00','2025-01-04T00:00:00',0,0,canonical(meta)))
        result=daily_quality(self.db,'EMPTY','precipitation')
        self.assertEqual([r['value'] for r in result],[0.,None,None])
        self.assertEqual([r['quality'] for r in result],['valid','missing','missing'])

    def test_quality_all_stopped_and_no_event_rows_keeps_missing_days(self):
        meta={'coverageConfirmed':True,'zeroForAbsent':True,
              'stoppedIntervals':[['2025-01-01T00:00:00','2025-01-04T00:00:00']]}
        with self.db.connect() as c:
            c.execute('INSERT INTO stations VALUES(?,?,?,?,?,?)',('STOPPED','stopped','rain',89.,32.,'{}'))
            c.execute('INSERT INTO sources VALUES(?,?,?,?,?,?)',('stopped','stopped','stopped',1,now(),'{}'))
            c.execute('INSERT INTO coverage VALUES(?,?,?,?,?,?,?,?,?)',('stopped','STOPPED','precipitation','irregular',
                '2025-01-01T00:00:00','2025-01-04T00:00:00',0,0,canonical(meta)))
        result=daily_quality(self.db,'STOPPED','precipitation')
        self.assertEqual([r['value'] for r in result],[None,None,None])
        self.assertEqual([r['quality'] for r in result],['missing','missing','missing'])


class EventHourPairingTests(unittest.TestCase):
    def test_occurrence_hour_endpoint_including_exact_hour_and_midnight(self):
        examples={'2025-01-01T00:00:00':'2025-01-01T01:00:00',
                  '2025-01-01T01:00:00':'2025-01-01T02:00:00',
                  '2025-01-01T01:30:00':'2025-01-01T02:00:00',
                  '2025-01-01T23:00:00':'2025-01-02T00:00:00'}
        for event,endpoint in examples.items():
            self.assertEqual(event_temperature_endpoint(event),pd.Timestamp(endpoint))

    def test_station_warm_rain_uses_same_hours_as_daily_denominator(self):
        stations=('SLC','DUJA','BLQ','115DB')
        locations=[{'id':s,'lon':89.+i,'lat':32.} for i,s in enumerate(stations)]
        events=[{'source_id':'confirmed','time':t,'step':'irregular','value':v,'quality':'valid'}
                for t,v in [('2025-01-01T00:00:00',.5),('2025-01-01T01:00:00',2.),
                            ('2025-01-01T01:30:00',4.),('2025-01-01T23:00:00',3.)]]
        class FakeDb:
            def rows(self,sql,args=()):
                return locations if 'FROM stations' in sql else events if args==('confirmed',) else []
            def observations(self,*args,**kwargs):return []
        times=pd.date_range('2025-01-01',periods=25,freq='h')
        temp=np.full((25,4),10.);temp[[0,2,23],:]=0.
        source={'times':times,'spatial':{'grids':locations},
                'arrays':{'temperature':temp,'precipitation':np.ones((25,4))},
                'manifest':{'dataSHA256':'forcing'}}
        coverage=[{'station':'SLC','sourceId':'confirmed','start':'2025-01-01T00:00:00',
                   'endExclusive':'2025-01-02T00:00:00'}]
        samples,_,_=station_samples(FakeDb(),source,coverage)
        row=next(r for r in samples['SLC']['days'] if r['date']==pd.Timestamp('2025-01-01'))
        self.assertEqual(row['source'],'confirmed-warm-events')
        self.assertEqual(row['obsWarm'],3.5)
        self.assertEqual(row['eraWarm'],22.)
        stats={station:[] for station in stations}
        new_profile=make_profile(source,samples,stats,coverage,'snapshot')
        old_profile=deepcopy(new_profile);old_profile.pop('eventTemperaturePairing')
        self.assertEqual(new_profile['eventTemperaturePairing'],EVENT_TEMPERATURE_PAIRING)
        self.assertNotEqual(digest(new_profile),digest(old_profile))


if __name__=='__main__':unittest.main()
