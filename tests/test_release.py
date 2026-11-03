import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.common import ROOT, VERSION, canonical, digest, write_json, sha_file
from backend.database import Database
from scripts.assemble_release import copy_calibration_baseline, copy_frontend_dist, inspect_seed, selected_resources


class ReleaseTests(unittest.TestCase):
    def test_calibration_baseline_copy_uses_registered_frozen_curve(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            operational=root/'运行程序/config/operational'
            source=operational/'calibration-baselines/JOB-baseline.json'
            frozen={'id':'JOB-baseline','status':'succeeded',
                    'result':{'identity':{'inputId':'input'},'daily':[{'date':'2025-01-01'}]}}
            write_json(source,frozen)
            write_json(operational/'calibration-baselines.json',{
                'schemaVersion':1,'results':{'JOB-baseline':{
                    'file':'calibration-baselines/JOB-baseline.json','sha256':sha_file(source)}}})
            destination=root/'release'
            with patch('scripts.assemble_release.ROOT',root):
                self.assertTrue(copy_calibration_baseline('JOB-baseline',destination))
                packaged=destination/'config/operational/calibration-baselines/JOB-baseline.json'
                self.assertEqual(json.loads(packaged.read_text(encoding='utf-8')),frozen)
                source.write_text('{}',encoding='utf-8')
                with self.assertRaises(ValueError):
                    copy_calibration_baseline('JOB-baseline',root/'tampered-release')

    def test_adopted_published_result_has_one_read_only_calculation_record(self):
        from backend.published_results import calculation_record
        published = {'id':'JOB-best','kind':'two-stage-calibration','status':'succeeded',
                     'created_at':'2026-11-01T00:00:00+00:00','updated_at':'2026-11-01T01:00:00+00:00',
                     'name':'色林错连续模拟方案','displayName':'色林错连续模拟方案',
                     'inputId':'INPUT-best','businessDefault':True,
                     'requestSummary':{'schemeName':'色林错连续模拟方案','inputId':'INPUT-best',
                                       'start':'2017-01-01T00:00:00','end':'2026-10-21T00:00:00',
                                       'modelFamily':'grid-continuous-v1'}}
        with patch('backend.published_results.catalog',return_value=[published]):
            row=calculation_record('JOB-best')
            self.assertIsNone(calculation_record('JOB-old'))
        self.assertEqual(row['id'],'JOB-best')
        self.assertEqual(row['kind'],'two-stage-calibration')
        self.assertEqual(row['status'],'succeeded')
        self.assertTrue(row['builtin'])
        self.assertTrue(row['userSaved'])
        self.assertEqual(row['request']['inputId'],'INPUT-best')
        self.assertEqual(row['request']['start'],'2017-01-01T00:00:00')
        self.assertIsNone(row['result'])

    def test_seed_uses_frozen_adopted_result_when_task_history_is_compacted(self):
        from scripts.make_seed import business_default
        from backend.common import canonical
        identity={'inputId':'INPUT-best','inputSHA256':'forcing-sha'}
        parameters={'WM':180.0}
        result={'inputId':'INPUT-best','spatialVersion':'SPACE-best','identity':identity,
                'parameters':parameters,'partitionParameters':{},'scheme':{'spatialVersion':'SPACE-best'}}
        selection={'schemeId':'SCHEME-best','parameterId':'PARAM-best','resultId':'JOB-best',
                   'inputId':'INPUT-best','spatialVersion':'SPACE-best'}
        scheme={'id':'SCHEME-best','payload':{'inputId':'INPUT-best','spatialVersion':'SPACE-best',
                 'parameters':parameters,'partitionParameters':{}}}
        parameter={'id':'PARAM-best','payload':{'parameters':parameters,'partitionParameters':{},
                    'resultIdentitySHA256':digest(identity)}}
        product={'id':'INPUT-best','manifest':{'dataSHA256':'forcing-sha','spatialVersion':'SPACE-best'}}

        class Source:
            def rows(self,query,values):
                if 'settings' in query:return [{'value':canonical(selection)}]
                return []
            def items(self,name):
                return {'schemes':[scheme],'parameter_versions':[parameter],
                        'input_products':[product]}[name]

        frozen={'id':'JOB-best','status':'succeeded','request':{'start':'2017-01-01'},'result':result}
        with patch('backend.published_results.catalog',return_value=[{'id':'JOB-best'}]), \
             patch('backend.published_results.detail',return_value=frozen):
            adopted=business_default(Source(),'current')
        self.assertEqual(adopted['task'],frozen)
        self.assertEqual(adopted['selection'],selection)
        self.assertEqual(adopted['resultIdentitySHA256'],digest(identity))

    def test_frontend_package_contains_only_manifest_assets_and_keeps_public_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root/'dist'
            assets = source/'assets'
            assets.mkdir(parents=True)
            for name in ('main.js','shared.js','lazy.js','main.css','font.woff2','old.js'):
                (assets/name).write_text(name)
            (source/'index.html').write_text('<script type="module" src="/assets/main.js"></script>')
            (source/'data').mkdir()
            (source/'data/domain.json').write_text('{}')
            write_json(source/'.vite/manifest.json', {
                'index.html': {'file':'assets/main.js','isEntry':True,'imports':['shared.js'],
                               'dynamicImports':['lazy.js'],'css':['assets/main.css'],'assets':['assets/font.woff2']},
                'shared.js': {'file':'assets/shared.js'},
                'lazy.js': {'file':'assets/lazy.js'},
            })
            destination = root/'package/frontend/dist'
            copy_frontend_dist(source,destination)
            self.assertTrue((destination/'index.html').is_file())
            self.assertTrue((destination/'data/domain.json').is_file())
            for name in ('main.js','shared.js','lazy.js','main.css','font.woff2'):
                self.assertTrue((destination/'assets'/name).is_file())
            self.assertFalse((destination/'assets/old.js').exists())
            self.assertFalse((destination/'.vite/manifest.json').exists())

    def test_resources_keep_selected_space_and_exclude_research_pointer_and_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = ['data/raw/observations/source.xlsx',
                     'data/derived/spatial/manifest.json',
                     'data/derived/spatial/grid-current.json',
                     'data/derived/spatial/terrain-current.json',
                     'data/derived/spatial/地形展示/terrain-1200.png',
                     'data/derived/spatial/地形展示/terrain-2400.png',
                     'data/derived/spatial/versions/standard/units.geojson',
                     'data/derived/spatial/versions/selected/units.geojson',
                     'data/derived/spatial/versions/research/units.geojson']
            for name in paths:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}')
            write_json(root / 'data/derived/spatial/manifest.json', {'version': 'base'})
            write_json(root / 'data/derived/spatial/terrain-current.json', {
                'image':'/spatial/地形展示/terrain-1200.png',
                'levels':[{'image':'/spatial/地形展示/terrain-1200.png'},
                          {'image':'/spatial/地形展示/terrain-2400.png'}]})
            write_json(root / 'data/derived/spatial/versions/standard/manifest.json', {'version': 'standard'})
            write_json(root / 'data/derived/spatial/versions/selected/manifest.json', {'version': 'selected'})
            write_json(root / 'data/derived/spatial/versions/research/manifest.json', {'version': 'research'})
            for name in paths[4:6]:(root/name).write_bytes(b'terrain')
            for directory, names in [('full_basin_shp',['basin.shp']),
                                     ('subbasin_shp',['one.shp','two.shp','three.shp','four.shp']),
                                     ('lake_shp',['lake.shp'])]:
                folder=root/'data/raw/spatial/source-package'/directory
                folder.mkdir(parents=True)
                for name in names:(folder/name).write_bytes(b'shapefile')
            dem=root/'slc_dem/slc_dem';dem.mkdir(parents=True)
            for name in ('dblbnd.adf','hdr.adf','prj.adf','sta.adf','vat.adf','w001001.adf','w001001x.adf'):
                (dem/name).write_bytes(b'dem')
            with patch('scripts.assemble_release.ROOT', root):
                actual = {p.relative_to(root).as_posix() for p in selected_resources(
                    {'spatialVersion': 'standard','businessDefault':{'spatialVersion':'standard'}},
                    [{'directory': 'chosen','spatialVersion':'selected'}])}
                self.assertNotIn(paths[0], actual)  # referenced observation originals live in the seed
                self.assertIn(paths[6], actual)
                self.assertIn(paths[7], actual)
                self.assertIn('slc_dem/slc_dem/w001001.adf',actual)
                self.assertIn('data/raw/spatial/source-package/subbasin_shp/four.shp',actual)
                self.assertIn(paths[2], actual)
                self.assertIn(paths[3], actual)
                self.assertIn(paths[4], actual)
                self.assertIn(paths[5], actual)
                self.assertNotIn(paths[8], actual)
                with self.assertRaises(ValueError):
                    selected_resources({'spatialVersion': 'missing'}, [])

    def test_seed_inspection_uses_actual_database_not_a_stale_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(tmp)
            write_json(Path(tmp)/'seed-report.json', {'projectId':'selinco','counts':{'input_products':1,'tasks':0}})
            with self.assertRaises(ValueError):
                inspect_seed(Path(tmp))
            with db.connect() as conn:
                conn.execute('INSERT INTO input_products VALUES(?,?,?,?)',('real','真实输入','2026-01-01','{}'))
                conn.execute('INSERT INTO tasks(id,kind,status,created_at,updated_at,request) VALUES(?,?,?,?,?,?)',
                             ('research','trial','succeeded','2026','2026','{}'))
            with self.assertRaises(ValueError):
                inspect_seed(Path(tmp))

    def test_clean_seed_is_read_only_and_requires_the_builtin_input(self):
        from backend.release_registry import SCHEMA
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(tmp)
            payload = Path(tmp)/'inputs/products/real/forcing.npz'
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b'read-only-input-hash-fixture')
            manifest={'data':'forcing.npz','dataSHA256':sha_file(payload),'spatialVersion':'standard'}
            result={'inputId':'real','identity':{'inputId':'real'},'scheme':{'id':'standard','spatialVersion':'standard'}}
            with db.connect() as conn:
                conn.execute('INSERT INTO input_products VALUES(?,?,?,?)',('real','真实输入','2026-01-01',json.dumps({'data':'forcing.npz','dataSHA256':sha_file(payload)})))
                conn.execute('UPDATE input_products SET manifest=? WHERE id=?',(json.dumps(manifest),'real'))
                conn.executescript(SCHEMA)
                conn.execute('INSERT INTO scheme_revisions VALUES(?,?)',('revision',json.dumps({'identity':result['identity'],'status':'protected-legacy'})))
                conn.execute('INSERT INTO operational_releases(id,scheme_revision_id,result_id,published_at,action) VALUES(?,?,?,?,?)',('release','revision','builtin','2026','protected-import'))
            write_json(Path(tmp)/'seed-report.json',{'schemaVersion':2,'resultId':'builtin','release':{'id':'release'},'resultIdentity':result['identity'],'defaultInputId':'real','inputManifest':manifest})
            before = db.path.read_bytes()
            with patch('backend.published_results.catalog',return_value=[{'id':'builtin'}]),patch('backend.published_results.detail',return_value={'result':result}):
                counts, scheme = inspect_seed(Path(tmp))
                self.assertEqual(counts['tasks'],0)
                self.assertEqual(scheme['id'],'standard')
            self.assertEqual(db.path.read_bytes(),before)
            with patch('backend.published_results.catalog',return_value=[]), self.assertRaises(ValueError):
                inspect_seed(Path(tmp))

    def test_installer_and_manifest_versions_match_current_runtime(self):
        installer = (ROOT/'installer/selinco.iss').read_text(encoding='utf-8-sig')
        self.assertIn(f'#define AppVersion "{VERSION}"',installer)
        self.assertIn('OutputBaseFilename=色林错预警预报平台-v{#AppVersion}-Setup',installer)
        self.assertEqual(json.loads((ROOT/'config/platform.json').read_text(encoding='utf-8-sig'))['version'],VERSION)
        build = (ROOT/'scripts/build_windows.ps1').read_text(encoding='utf-8-sig')
        self.assertIn('tests/history-workflow.test.mjs',build)
        self.assertIn('verify_delivery.ps1',build)
        self.assertIn('verify_installation.ps1',build)
        self.assertIn('scripts/compile_installer.py',build)
        self.assertIn('--verification',build)
        self.assertIn('Restore packaged business workspace pointer failed',build)

    def test_release_workspace_pointer_is_business_default_after_acceptance(self):
        release=ROOT/'build/release-20261103-033530/SelincoPlatform-final'
        if not (release/'release-manifest.json').is_file():
            self.skipTest('release worktree is not present')
        config=json.loads((release/'config/active-workspace.json').read_text(encoding='utf-8'))
        self.assertEqual(config,{'workspace':'workspace-business','purpose':'business-default','seed':'seed/workspace'})

    def test_business_seed_keeps_protected_release_and_one_adopted_scheme(self):
        from backend.release_registry import SCHEMA
        with tempfile.TemporaryDirectory() as tmp:
            db=Database(tmp)
            legacy_input=Path(tmp)/'inputs/products/legacy/forcing.npz'
            business_input=Path(tmp)/'inputs/products/best/forcing.npz'
            legacy_input.parent.mkdir(parents=True);business_input.parent.mkdir(parents=True)
            legacy_input.write_bytes(b'legacy');business_input.write_bytes(b'best')
            legacy_manifest={'id':'legacy','data':'forcing.npz','dataSHA256':sha_file(legacy_input),'spatialVersion':'legacy-space'}
            business_manifest={'id':'best','projectId':'selinco','data':'forcing.npz','dataSHA256':sha_file(business_input),
                               'spatialVersion':'best-space'}
            result_identity={'inputId':'legacy'}
            legacy_result={'inputId':'legacy','identity':result_identity,
                           'scheme':{'id':'legacy-scheme','spatialVersion':'legacy-space'},
                           'parameters':{'KC':.6},'partitionParameters':{}}
            payload={'schemaVersion':2,'id':'best-scheme','name':'色林错连续模拟方案','inputId':'best',
                     'spatialVersion':'best-space','parameters':{'KC':.7},'partitionParameters':{},'purpose':'business'}
            parameter={'parameters':payload['parameters'],'partitionParameters':{},
                       'resultIdentitySHA256':digest({'best':True})}
            selection={'schemeId':'best-scheme','familyId':'family-best','parameterId':'best-parameter',
                       'resultId':'JOB-best','inputId':'best','spatialVersion':'best-space','name':payload['name']}
            with db.connect() as connection:
                connection.execute('INSERT INTO input_products VALUES(?,?,?,?)',
                                   ('legacy','legacy','2026',canonical(legacy_manifest)))
                connection.execute('INSERT INTO input_products VALUES(?,?,?,?)',
                                   ('best','best','2026',canonical(business_manifest)))
                connection.execute('INSERT INTO schemes VALUES(?,?,?,?)',
                                   ('best-scheme',payload['name'],'2026',canonical(payload)))
                connection.execute('INSERT INTO parameter_versions VALUES(?,?,?,?)',
                                   ('best-parameter','best parameter','2026',canonical(parameter)))
                connection.execute('INSERT INTO settings VALUES(?,?)',
                                   ('business-adopted-scheme-v1',canonical(selection)))
                connection.executescript(SCHEMA)
                connection.execute('INSERT INTO scheme_revisions VALUES(?,?)',
                                   ('legacy-revision',canonical({'identity':result_identity,'status':'protected-legacy',
                                                                 'admissionPassed':False})))
                connection.execute('INSERT INTO operational_releases(id,scheme_revision_id,result_id,published_at,action) VALUES(?,?,?,?,?)',
                                   ('legacy-release','legacy-revision','BUILTIN-legacy','2026','protected-import'))
            business={'schemeId':'best-scheme','familyId':'family-best','parameterId':'best-parameter',
                      'resultId':'JOB-best','name':payload['name'],'inputId':'best','spatialVersion':'best-space',
                      'resultIdentitySHA256':digest({'best':True}),
                      'schemePayloadSHA256':digest(payload),'parameterPayloadSHA256':digest(parameter),
                      'inputManifestSHA256':digest(business_manifest)}
            write_json(Path(tmp)/'workspace-identity.json',{'projectId':'selinco'})
            write_json(Path(tmp)/'seed-report.json',{'schemaVersion':2,'resultId':'BUILTIN-legacy',
                       'release':{'id':'legacy-release'},'resultIdentity':result_identity,
                       'defaultInputId':'legacy','inputManifest':legacy_manifest,'businessDefault':business})
            before=db.path.read_bytes()
            with patch('backend.published_results.catalog',return_value=[{'id':'BUILTIN-legacy'}]), \
                 patch('backend.published_results.detail',return_value={'result':legacy_result}):
                counts,scheme=inspect_seed(Path(tmp))
            self.assertEqual(counts['schemes'],1)
            self.assertEqual(counts['parameter_versions'],1)
            self.assertEqual(scheme['release']['status'],'protected-legacy')
            self.assertFalse(scheme['release']['admissionPassed'])
            self.assertEqual(scheme['businessDefault']['schemeId'],'best-scheme')
            self.assertEqual(db.path.read_bytes(),before)
