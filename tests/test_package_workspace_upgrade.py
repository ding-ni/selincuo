import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.common import canonical, digest, sha_file, write_json
from backend.database import Database
from backend.workspace_upgrade import prepare_package_workspace


class PackageWorkspaceUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='package-default-upgrade-')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.app=self.root/'app';self.seed=self.app/'seed/workspace'
        self.business=self.make_seed()
        version=self.business['spatialVersion']
        folder=self.app/'data/derived/spatial/versions'/version
        folder.mkdir(parents=True)
        write_json(folder/'manifest.json',{'version':version})

    def make_seed(self):
        db=Database(self.seed)
        input_id='INPUT-package-best';scheme_id='SCHEME-package-best'
        parameter_id='PARAM-package-best';result_id='JOB-package-best';spatial='AGG-package-best'
        data=self.seed/'inputs/products'/input_id/'forcing.npz'
        data.parent.mkdir(parents=True)
        data.write_bytes(b'complete-hourly-forcing-fixture')
        manifest={'id':input_id,'projectId':'selinco','data':'forcing.npz','dataSHA256':sha_file(data),
                  'spatialVersion':spatial,'start':'2017-01-01T00:00:00','end':'2026-10-21T00:00:00'}
        write_json(data.parent/'manifest.json',manifest)
        params={'KC':0.7,'WM':180.0};partition={}
        scheme={'schemaVersion':2,'id':scheme_id,'name':'色林错连续模拟方案','inputId':input_id,
                'spatialVersion':spatial,'parameters':params,'partitionParameters':partition,'purpose':'business'}
        parameter={'parameters':params,'partitionParameters':partition,'resultIdentitySHA256':'identity'}
        selection={'schemeId':scheme_id,'familyId':'FAMILY-package-best','parameterId':parameter_id,
                   'resultId':result_id,'inputId':input_id,'spatialVersion':spatial,'name':scheme['name']}
        with db.connect() as connection:
            connection.execute('INSERT INTO input_products VALUES(?,?,?,?)',
                               (input_id,'完整驱动','2026-11-02',canonical(manifest)))
            connection.execute('INSERT INTO schemes VALUES(?,?,?,?)',
                               (scheme_id,scheme['name'],'2026-11-02',canonical(scheme)))
            connection.execute('INSERT INTO parameter_versions VALUES(?,?,?,?)',
                               (parameter_id,scheme['name']+'参数','2026-11-02',canonical(parameter)))
            connection.execute('INSERT INTO settings VALUES(?,?)',
                               ('business-adopted-scheme-v1',canonical(selection)))
            connection.execute('INSERT INTO settings VALUES(?,?)',
                               ('current-model-forcing',canonical({'inputId':input_id,
                                'source':'packaged-business-default'})))
            connection.execute('INSERT INTO settings VALUES(?,?)',
                               ('scheme-purpose:'+selection['familyId'],canonical('business')))
        business={'schemeId':scheme_id,'familyId':selection['familyId'],'parameterId':parameter_id,
                  'resultId':result_id,'name':scheme['name'],'inputId':input_id,'spatialVersion':spatial,
                  'schemePayloadSHA256':digest(scheme),'parameterPayloadSHA256':digest(parameter),
                  'resultIdentitySHA256':digest({'inputId':input_id}),
                  'inputManifestSHA256':digest(manifest)}
        write_json(self.seed/'seed-report.json',{'schemaVersion':2,'projectId':'selinco',
                   'businessDefault':business})
        return business

    def test_fresh_install_copies_complete_frozen_seed(self):
        workspace=self.root/'fresh/workspace'
        result=prepare_package_workspace(workspace,self.seed,self.app)
        self.assertEqual(result['status'],'seeded')
        db=Database(workspace)
        self.assertEqual(db.setting('business-adopted-scheme-v1')['resultId'],self.business['resultId'])
        self.assertEqual([row['id'] for row in db.items('schemes')],[self.business['schemeId']])
        self.assertEqual([row['id'] for row in db.items('parameter_versions')],[self.business['parameterId']])
        self.assertEqual(db.items('input_products')[0]['id'],self.business['inputId'])
        self.assertEqual(db.setting('current-model-forcing')['inputId'],self.business['inputId'])

    def test_frozen_database_startup_bootstraps_packaged_seed(self):
        workspace=self.root/'frozen-install/workspace'
        with patch('backend.database.sys.frozen',True,create=True), \
             patch('backend.database.WORKSPACE',workspace), \
             patch('backend.workspace_upgrade.ROOT',self.app):
            db=Database()
        self.assertEqual(db.setting('business-adopted-scheme-v1')['resultId'],self.business['resultId'])
        self.assertTrue((workspace/'inputs/products'/self.business['inputId']/'forcing.npz').is_file())

    def test_upgrade_preserves_user_history_and_adoption_then_repeats_cleanly(self):
        workspace=self.root/'existing/workspace';db=Database(workspace)
        old_selection={'schemeId':'SCHEME-user','familyId':'FAMILY-user','parameterId':'PARAM-user',
                       'resultId':'JOB-user','inputId':'INPUT-user','spatialVersion':'old'}
        with db.connect() as connection:
            connection.execute('INSERT INTO tasks(id,kind,status,created_at,updated_at,request,result) VALUES(?,?,?,?,?,?,?)',
                               ('JOB-history','simulation','succeeded','2025','2025','{}','{}'))
            connection.execute('INSERT INTO settings VALUES(?,?)',
                               ('business-adopted-scheme-v1',canonical(old_selection)))
            connection.execute('INSERT INTO settings VALUES(?,?)',
                               ('current-model-forcing',canonical({'inputId':'INPUT-user'})))
        first=prepare_package_workspace(workspace,self.seed,self.app)
        self.assertEqual(first['status'],'supplemented')
        self.assertTrue(first['adoptionPreserved'])
        self.assertTrue(Path(first['backup']).is_file())
        self.assertEqual(db.setting('business-adopted-scheme-v1'),old_selection)
        self.assertEqual(db.setting('current-model-forcing')['inputId'],self.business['inputId'])
        self.assertEqual(db.task('JOB-history')['status'],'succeeded')
        self.assertTrue((workspace/'inputs/products'/self.business['inputId']/'forcing.npz').is_file())
        self.assertEqual(len(db.items('schemes')),1)
        second=prepare_package_workspace(workspace,self.seed,self.app)
        self.assertEqual(second['status'],'already-present')
        self.assertFalse(second['changed'])
        self.assertEqual(len(db.rows("SELECT * FROM audit WHERE action='workspace.business-default-supplement'")),1)

    def test_package_identity_conflict_fails_without_overwriting_user_rows(self):
        workspace=self.root/'existing/workspace';db=Database(workspace)
        conflict={'schemaVersion':2,'name':'user version','inputId':self.business['inputId'],
                  'spatialVersion':self.business['spatialVersion']}
        with db.connect() as connection:
            connection.execute('INSERT INTO schemes VALUES(?,?,?,?)',
                               (self.business['schemeId'],'existing','old',canonical(conflict)))
        with self.assertRaisesRegex(ValueError,'冲突'):
            prepare_package_workspace(workspace,self.seed,self.app)
        self.assertEqual(db.items('schemes')[0]['payload'],conflict)
        self.assertFalse(db.items('input_products'))


if __name__=='__main__':
    unittest.main()
