import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from backend.common import sha_file, write_json
from backend import business_context

class PackagedCalibrationBaselineTests(unittest.TestCase):
    def test_baseline_loads_and_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); folder=root/'config/operational'; identifier='JOB-baseline'
            path=folder/'calibration-baselines'/f'{identifier}.json'
            original={'id':identifier,'status':'succeeded','kind':'calibration-comparison',
                      'result':{'identity':{'model':'x'},'daily':[{'date':'2021-01-01','lakeLevel':4544}]}}
            write_json(path,original)
            write_json(folder/'calibration-baselines.json',{'schemaVersion':1,'results':{identifier:{
                'file':f'calibration-baselines/{identifier}.json','sha256':sha_file(path)}}})
            class DB:
                def task(self, value): raise AssertionError('baseline must not require research task')
            with patch.object(business_context,'ROOT',root),patch('backend.published_results.catalog',return_value=[]):
                self.assertEqual(business_context.result_task(DB(),identifier),original)
                write_json(path,{**original,'id':'wrong'})
                with self.assertRaisesRegex(ValueError,'摘要'):
                    business_context.result_task(DB(),identifier)
                write_json(folder/'calibration-baselines.json',{'schemaVersion':1,'results':{identifier:{
                    'file':f'calibration-baselines/{identifier}.json','sha256':sha_file(path)}}})
                with self.assertRaisesRegex(ValueError,'内容无效'):
                    business_context.result_task(DB(),identifier)

if __name__ == '__main__': unittest.main()
