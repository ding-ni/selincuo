import tempfile
from contextlib import nullcontext
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
from backend.common import digest,write_json
from backend.model_core import DEFAULT_PARAMETERS
from backend.grid_simulation import run
from backend.observation_profiles import daily_quality,_paired_points


class GridExportTests(unittest.TestCase):
    def test_simultaneous_downloads_share_one_complete_artifact(self):
        from concurrent.futures import ThreadPoolExecutor
        from backend.grid_storage import Writer, export_npz
        with nullcontext(tempfile.mkdtemp(prefix='selinco-grid-check-')) as tmp:
            folder = Path(tmp)
            states = np.arange(25*2*8, dtype=float).reshape(25,2,8)
            writer = Writer(folder,25,2,8)
            writer.put(0,25,{'time':np.arange(25,dtype=np.int64),'unitStates':states})
            writer.finish()
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(export_npz,[folder,folder]))
            self.assertEqual(results[0],results[1])
            with np.load(results[0],allow_pickle=False) as saved:
                np.testing.assert_array_equal(saved['unitStates'],states)
                np.testing.assert_array_equal(saved['time'],np.arange(25))


class ObservationQualityTests(unittest.TestCase):
    def rows(self,times,values=None,step='irregular'):
        return [{'id':i,'time':t.isoformat(),'value':float(values[i] if values is not None else i),'quality':'valid','step':step} for i,t in enumerate(times)]

    def daily(self,rows):
        with patch('backend.observation_profiles.revision',return_value=object()),patch('backend.observation_profiles.SeriesView') as view:
            view.return_value.observations.return_value=rows
            return daily_quality(None,'SLC')

    def test_hourly_and_three_hourly_time_integrals(self):
        for hours in (1,3):
            times=pd.date_range('2025-01-01',periods=24//hours+1,freq=str(hours)+'h')
            day=self.daily(self.rows(times,np.arange(len(times))*hours))[0]
            self.assertEqual(day['value'],12.)
            self.assertEqual(day['coverageHours'],24.)
            self.assertEqual(day['maximumGapHours'],hours)

    def test_long_gap_and_insufficient_coverage_stay_missing(self):
        day=self.daily(self.rows(pd.to_datetime(['2025-01-01T00:00','2025-01-01T06:00','2025-01-01T18:00','2025-01-02T00:00'])))[0]
        self.assertIsNone(day['value']);self.assertEqual(day['coverageHours'],12.)
        self.assertEqual(day['maximumGapHours'],12.)

    def test_original_valid_daily_sheet_precedes_reconstruction(self):
        rows=self.rows(pd.date_range('2025-01-01',periods=9,freq='3h'))
        rows+=self.rows(pd.to_datetime(['2025-01-01']),[123.4],step='day')
        day=self.daily(rows)[0]
        self.assertEqual(day['source'],'source-daily');self.assertEqual(day['value'],123.4)

    def test_nearby_matching_does_not_reuse_main_observation(self):
        main=self.rows(pd.to_datetime(['2025-01-01T12:00','2025-01-01T13:00']),[10,11])
        shore=self.rows(pd.to_datetime(['2025-01-01T11:55','2025-01-01T12:02','2025-01-01T13:20']),[10.5,10.6,11.6])
        with patch('backend.observation_profiles.SeriesView') as view:
            view.return_value.observations.side_effect=lambda station,variable:main if station=='SLC' else shore
            pairs=_paired_points(None,'SLC-N')
        self.assertEqual(len(pairs),1);self.assertAlmostEqual(pairs.difference.iloc[0],.6)


class ContinuousGridTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        times=pd.date_range('2024-12-29',periods=169,freq='h')
        units=[{'id':str(i),'partition':part,'basin':part,'gridIndex':i,'areaKm2':10.,'elevation':5000.,'kind':'land','elevationBandMin':5000,'elevationBandMax':5500} for i,part in enumerate(('duja','unresolved'))]
        self.source={'times':times,'manifest':{'id':'INPUT-test','dataSHA256':'f','transformationSHA256':'t'},'spatial':{'version':'GRID-test','units':units,'grids':[{'referenceElevation':5000.}]*2,'forcingGridVersion':'g','lakeWeights':[.5,.5],'lakeAreaKm2':20.,'drainage':{'inflowComplete':False}},'arrays':{'precipitation':np.ones((169,2))*.3,'temperature':np.ones((169,2))*5.,'evaporation':np.ones((169,2))*.05}}
        self.source['manifest']['start']=times[0].isoformat()
        workspace=Path(tempfile.mkdtemp(prefix='selinco-grid-continuation-'))
        write_json(workspace/'inputs/products/INPUT-test/manifest.json',self.source['manifest'])
        self.db=SimpleNamespace(workspace=workspace,items=lambda table:[{'id':'INPUT-test','manifest':self.source['manifest']}])
        self.request={'inputId':'INPUT-test','spatialVersion':'GRID-test','start':times[0].isoformat(),'end':times[-1].isoformat(),'parameters':deepcopy(DEFAULT_PARAMETERS),'_observations':{'profile':'test-quality','level':{'2024-12-30T00:00:00':4544.},'flow':{}},'_modelIdentity':'numerical-test'}

    def test_continuity_across_new_year_missing_observations_and_split(self):
        full=run(self.db,self.request,prepared=self.source)
        self.assertEqual(full['initialization']['time'],'2024-12-30T12:00:00')
        self.assertIsNone(full['daily'][0]['lakeLevel'])
        self.assertTrue(all(r['lakeLevel'] is not None for r in full['daily'] if r['date']>='2024-12-31'))
        first=run(self.db,{**self.request,'end':self.source['times'][72].isoformat()},prepared=self.source)
        second=run(self.db,{**self.request,'start':self.source['times'][73].isoformat(),'_parentState':first['checkpoint']},prepared=self.source)
        np.testing.assert_allclose(full['checkpoint']['unitStates'],second['checkpoint']['unitStates'],atol=1e-12)
        self.assertAlmostEqual(full['checkpoint']['lake']['level'],second['checkpoint']['lake']['level'],places=10)
        self.assertEqual(full['checkpoint']['routing'],second['checkpoint']['routing'])
        self.assertGreater(full['daily'][-1]['excludedRunoff'],0)

    def test_changed_prefix_parameters_unit_order_and_missing_queue_rejected(self):
        first=run(self.db,{**self.request,'end':self.source['times'][72].isoformat()},prepared=self.source)
        request={**self.request,'start':self.source['times'][73].isoformat(),'_parentState':first['checkpoint']}
        for kind in ('prefix','parameter','queue','unit'):
            bad=deepcopy(request);source=deepcopy(self.source)
            if kind=='prefix':source['arrays']['precipitation'][0,0]+=1;source['manifest']['dataSHA256']='revised'
            if kind=='parameter':bad['parameters']['KC']+=.01
            if kind=='queue':bad['_parentState']['routing']['duja']['channel'].pop('queue')
            if kind=='unit':bad['_parentState']['unitIds'].reverse()
            with self.subTest(kind=kind),self.assertRaises(ValueError):run(self.db,bad,prepared=source)

    def test_earlier_added_hours_keep_old_prefix_and_revised_overlap_is_rejected(self):
        full=run(self.db,self.request,prepared=self.source)
        first=run(self.db,{**self.request,'end':self.source['times'][72].isoformat()},prepared=self.source)
        backfill=deepcopy(self.source)
        backfill['times']=pd.date_range(self.source['times'][0]-pd.Timedelta(hours=48),periods=217,freq='h')
        for key,array in backfill['arrays'].items():backfill['arrays'][key]=np.concatenate([np.repeat(array[:1],48,axis=0),array])
        backfill['manifest']['id']='INPUT-backfill';backfill['manifest']['dataSHA256']='backfill'
        request={**self.request,'inputId':'INPUT-backfill','start':self.source['times'][73].isoformat(),'_parentState':first['checkpoint']}
        second=run(self.db,request,prepared=backfill)
        np.testing.assert_allclose(full['checkpoint']['unitStates'],second['checkpoint']['unitStates'],atol=1e-12)
        self.assertAlmostEqual(full['checkpoint']['lake']['level'],second['checkpoint']['lake']['level'],places=10)
        self.assertEqual(full['checkpoint']['routing'],second['checkpoint']['routing'])
        revised=deepcopy(backfill);revised['manifest']['dataSHA256']='revision';revised['arrays']['precipitation'][48,0]+=1
        with self.assertRaisesRegex(ValueError,'历史气象资料已修订'):run(self.db,request,prepared=revised)

    def test_lake_only_correction_applied_once_and_land_unchanged(self):
        baseline=run(self.db,self.request,prepared=self.source)
        corrected=deepcopy(self.source)
        corrected['manifest']['lakePrecipitationCorrection']={'method':'station-point-annual-ratio-v1','scope':'lake-only','factor':0.5}
        corrected['manifest']['transformationSHA256']='half'
        result=run(self.db,self.request,prepared=corrected)
        np.testing.assert_array_equal(baseline['checkpoint']['unitStates'],result['checkpoint']['unitStates'])
        self.assertAlmostEqual(result['daily'][1]['lakeRainMm'],baseline['daily'][1]['lakeRainMm']*0.5)
        self.assertAlmostEqual(baseline['daily'][1]['lakeRainMm'],7.2)

    def test_zarr_query_and_npz_export_preserve_float64(self):
        from backend.grid_storage import open_arrays,export_npz
        with nullcontext(tempfile.mkdtemp(prefix='selinco-grid-export-check-')) as temp:
            folder=Path(temp);result=run(self.db,self.request,folder,prepared=self.source)
            arrays=open_arrays(folder)
            self.assertEqual(arrays['unitStates'].dtype,np.dtype('float64'))
            np.testing.assert_array_equal(arrays['unitStates'][-1],result['checkpoint']['unitStates'])
            with np.load(export_npz(folder)) as saved:np.testing.assert_array_equal(saved['unitStates'][-1],arrays['unitStates'][-1])

class SpaceAggregationTests(unittest.TestCase):
    def test_daily_flux_and_flow_frames_share_the_process_convention(self):
        from backend.result_query import read
        times=pd.date_range('2024-01-01',periods=25,freq='h')
        task={'result':{'units':[{'id':'U'}],'rivers':['duja']}}
        data={'time':times.asi8,'unitDiagnostics':np.repeat(np.arange(25.)[:,None,None],9,axis=2),'unitStates':np.repeat(np.arange(25.)[:,None,None],8,axis=2),'flow':np.arange(25.)[:,None]}
        entry={'task':task,'data':data,'state':{},'times':times,
               'initializationDays':None,'initializationDaysBuilt':False}
        with patch('backend.result_query._entry',return_value=entry):
            q={'resultId':'R','time':'2024-01-02T00:00:00','step':'day'}
            self.assertEqual(read(None,{**q,'variable':'RS'},frame=True)['values'][0]['value'],sum(range(1,25)))
            self.assertEqual(read(None,{**q,'variable':'flow'},frame=True)['values'][0]['value'],12.5)
            self.assertEqual(read(None,{**q,'variable':'swe'},frame=True)['values'][0]['value'],24.)
            self.assertEqual(read(None,{**q,'variable':'RS','step':'hour'},frame=True)['values'][0]['value'],24.)

if __name__=='__main__':unittest.main()
