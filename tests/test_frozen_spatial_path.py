"""Spatial metadata must use installed app assets rather than frozen __file__."""
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd

BASE=Path(__file__).resolve().parents[1]
STAGE=BASE if (BASE/'backend/result_query.py').is_file() else BASE/'stage'
APP=Path(os.environ.get('SELINCO_TEST_APP_ROOT',str(BASE if (BASE/'backend/common.py').is_file() else 'F:/色林错预警预报平台')))
os.environ.setdefault('SELINCO_APP_ROOT',str(APP))
if (STAGE/'backend/result_query.py').is_file() and not (STAGE/'backend/common.py').is_file():
    package=sys.modules.get('backend') or types.ModuleType('backend')
    package.__path__=[str(STAGE/'backend'),str(APP/'backend')]
    sys.modules['backend']=package
elif str(APP) not in sys.path:sys.path.insert(0,str(APP))
from backend import result_query
from backend import grid_spatial


class FrozenSpatialPathTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='frozen-spatial-')
        self.root=Path(self.temp.name)/'installed-app'
        self.root.mkdir()
        self.root_patch=patch.object(result_query,'ROOT',self.root);self.root_patch.start()
        self.file_patch=patch.object(result_query,'__file__',str(self.root/'_internal/backend/result_query.py'));self.file_patch.start()

    def tearDown(self):
        self.file_patch.stop();self.root_patch.stop();self.temp.cleanup()

    def manifest(self,relative,value):
        path=self.root/'data/derived/spatial'/relative
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(value),encoding='utf-8')

    def entry(self,task,arrays,state=None):
        return {'task':task,'data':arrays,'state':state,
                'times':pd.DatetimeIndex(arrays['time'].astype('datetime64[ns]')),
                'initializationDays':None,'initializationDaysBuilt':False}

    def test_frozen_metadata_uses_app_root_archived_research_geometry(self):
        version='GRID-SPATIAL-research'
        self.manifest('versions/'+version+'/manifest.json',{'version':version,'schemaVersion':2})
        task={'id':'JOB-fixture','builtin':True,'result':{'units':[],'spatialVersion':version},'request':{}}
        arrays={'time':pd.date_range('2024-01-01',periods=2,freq='h').values.astype('int64'),
                'lakeLevel':np.array([4544.,4544.])}
        with patch.object(result_query,'_entry',return_value=self.entry(task,arrays)),patch.object(grid_spatial,'load',return_value={}):
            metadata=result_query.metadata(None,'JOB-fixture')
        self.assertEqual(metadata['spatialBase'],'/spatial/versions/'+version+'/')

    def test_missing_archive_rejects_different_current_geometry(self):
        self.manifest('manifest.json',{'version':'SPATIAL-other','schemaVersion':1})
        with self.assertRaisesRegex(FileNotFoundError,'不能使用其他版本替代'):
            result_query.spatial_base('GRID-SPATIAL-missing')

    def test_only_identical_legacy_current_manifest_can_fall_back(self):
        self.manifest('manifest.json',{'version':'SPATIAL-legacy','schemaVersion':1})
        self.assertEqual(result_query.spatial_base('SPATIAL-legacy'),'/spatial/')
        self.manifest('manifest.json',{'version':'GRID-SPATIAL-current','schemaVersion':2})
        with self.assertRaises(FileNotFoundError):result_query.spatial_base('GRID-SPATIAL-current')

    def test_no_version_keeps_legacy_fallback_and_corrupt_archive_rejected(self):
        self.assertEqual(result_query.spatial_base(None),'/spatial/')
        self.manifest('versions/GRID-requested/manifest.json',{'version':'GRID-actual','schemaVersion':2})
        with self.assertRaisesRegex(ValueError,'身份不一致'):result_query.spatial_base('GRID-requested')

    def test_older_result_uses_scheme_identity_and_conflicts_reject(self):
        version='SPATIAL-legacy-scheme'
        self.manifest('versions/'+version+'/manifest.json',{'version':version,'schemaVersion':1})
        result={'scheme':{'spatialVersion':version}}
        self.assertEqual(result_query.spatial_base(result_query.frozen_spatial_version(result)),
                         '/spatial/versions/'+version+'/')
        result['identity']={'spatialVersion':'SPATIAL-conflicting'}
        with self.assertRaisesRegex(ValueError,'互相冲突'):result_query.frozen_spatial_version(result)
        with self.assertRaisesRegex(ValueError,'互相冲突'):
            result_query.frozen_spatial_version({'spatialVersion':version},{'identity':{'spatialVersion':'OTHER'}})

    def test_versionless_metadata_requires_matching_legacy_unit_snapshot(self):
        units=[{'id':'legacy-unit','partition':'duja','areaKm2':1}]
        self.manifest('manifest.json',{'version':'SPATIAL-current','schemaVersion':1,'units':units})
        task={'id':'JOB-old','builtin':True,'result':{'units':units},'request':{}}
        arrays={'time':pd.date_range('2024-01-01',periods=2,freq='h').values.astype('int64'),
                'lakeLevel':np.array([4544.,4544.])}
        with patch.object(result_query,'_entry',return_value=self.entry(task,arrays)),patch.object(grid_spatial,'load',return_value={}):
            self.assertEqual(result_query.metadata(None,'JOB-old')['spatialBase'],'/spatial/')
            task['result']['units']=[{'id':'other-unit'}]
            with self.assertRaisesRegex(FileNotFoundError,'无法证明'):
                result_query.metadata(None,'JOB-old')


if __name__=='__main__':unittest.main(verbosity=2)
