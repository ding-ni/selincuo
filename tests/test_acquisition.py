import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
import xarray as xr

from backend.acquisition import acquire, inspect_file, inventory, requested_months, classify_error
from backend.common import digest


class AcquisitionTests(unittest.TestCase):
    def make_file(self, path, missing_hour=False, variables=('t2m', 'tp', 'pev')):
        times = pd.date_range('2024-02-01', '2024-02-29T23:00', freq='h')
        if missing_hour:
            times = times.delete(50)
        coords = {'valid_time': times, 'latitude': np.linspace(33.5, 29.9, 37), 'longitude': np.linspace(87.2, 92.4, 53)}
        ds = xr.Dataset({v: (('valid_time','latitude','longitude'), np.zeros((len(times),37,53),dtype=np.float32),
                            {'units':'K' if v=='t2m' else 'm','GRIB_stepType':'instant' if v=='t2m' else 'accum'}) for v in variables}, coords=coords)
        ds.to_netcdf(path, engine='h5netcdf', encoding={v:{'zlib':True} for v in variables})
        ds.close()

    def test_month_coverage_reads_content_instead_of_filename(self):
        with tempfile.TemporaryDirectory() as temp:
            w = Path(temp); folder=w/'downloads/era5-land';folder.mkdir(parents=True)
            self.make_file(folder/'era5-land-202401.nc')
            report=inventory(w, [2024], [1,2], False)
            self.assertFalse(report['months'][0]['complete'])
            self.assertTrue(report['months'][1]['complete'])
            self.assertEqual(report['completeMonths'],1)
            # Three variables must all have every hour, including leap day.
            self.make_file(folder/'gap.nc',True)
            self.assertEqual(inspect_file(folder/'gap.nc')['variables']['temperature'],[])
            self.make_file(folder/'temperature-only.nc',variables=('t2m',))
            self.assertNotIn('precipitation',inspect_file(folder/'temperature-only.nc')['variables'])

    def test_disabled_account_stops_after_one_bounded_probe(self):
        with tempfile.TemporaryDirectory() as temp, patch('cdsapi.Client') as constructor:
            client=constructor.return_value
            client.client.get_jobs.side_effect=RuntimeError('User has been disabled. https://signed.example/secret')
            with self.assertRaisesRegex(ValueError,'账户已被停用'):
                acquire(temp,[2024],[1,2],False,direct=True)
            self.assertEqual(client.client.get_jobs.call_count,1)
            client.retrieve.assert_not_called()
            saved=(Path(temp)/'downloads/era5-land/download-status.json').read_text(encoding='utf-8')
            self.assertNotIn('secret',saved)
            self.assertEqual(json.loads(saved)['status'],'account-disabled')

    def test_completed_month_is_idempotent_and_never_submitted(self):
        with tempfile.TemporaryDirectory() as temp, patch('cdsapi.Client') as constructor:
            folder=Path(temp)/'downloads/era5-land';folder.mkdir(parents=True)
            self.make_file(folder/'actual.nc')
            result=acquire(temp,[2024],[2],False)
            self.assertEqual(result['status'],'downloaded')
            constructor.assert_not_called()

    def test_resume_rejects_corrupt_chunks_and_preserves_incomplete_original(self):
        with tempfile.TemporaryDirectory() as temp, patch('cdsapi.Client') as constructor:
            w=Path(temp);folder=w/'downloads/era5-land';folder.mkdir(parents=True)
            original=w/'reference.nc';self.make_file(original)
            content=original.read_bytes()
            prepared=acquire(w,[2024],[2],False,prepare_only=True)
            plan=json.loads((folder/'download-requests.json').read_text(encoding='utf-8'))['requests'][0]
            target=folder/'era5-land-202402.nc';target.write_bytes(b'incomplete original')
            request_id='stable-remote-id'
            identity=digest({'requestId':request_id,'size':len(content),'request':plan['request']})[:24]
            chunks=folder/'.parts'/identity;chunks.mkdir(parents=True)
            (chunks/'0').write_bytes(b'x'*min(len(content),512*1024))
            (chunks/'0.sha256').write_text('not the correct digest')
            remote=Mock();remote.request_id=request_id;remote.status='successful'
            remote.get_results.return_value=Mock(content_length=len(content),location='https://example.invalid/result')
            client=constructor.return_value;client.retrieve.return_value=remote
            client.client.get_jobs.return_value=Mock(json={'jobs':[]})
            calls=[]
            class Transport:
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def close(self):pass
                def get(self,url,headers,timeout):
                    calls.append(headers['Range'])
                    a,b=map(int,headers['Range'][6:].split('-'))
                    response=Mock(status_code=206,headers={'Content-Range':f'bytes {a}-{b}/{len(content)}'},content=content[a:b+1])
                    response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
                    return response
            with patch('requests.Session',Transport):
                result=acquire(w,[2024],[2],False,direct=True)
            self.assertEqual(result['status'],'downloaded')
            self.assertTrue(calls)
            self.assertEqual(target.read_bytes(),b'incomplete original')
            self.assertEqual(len(list(folder.glob('era5-land-202402-*.nc'))),1)

    def test_halo_tracks_first_requested_month_and_separates_failure_causes(self):
        self.assertEqual(requested_months([2024],[5],True),['2024-04','2024-05'])
        self.assertEqual(len(requested_months()),37)
        self.assertEqual(classify_error(Exception('Maximum number queued requests'))[0],'queue-limit')
        self.assertEqual(classify_error(Exception('required licences'))[0],'license-required')
        self.assertEqual(classify_error(Exception('401 Client Error'))[0],'authentication')
        with self.assertRaises(ValueError): requested_months([2024],[13])

