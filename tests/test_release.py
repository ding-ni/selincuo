import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.common import ROOT, VERSION, canonical, digest, write_json, sha_file
from backend.database import Database
from scripts.assemble_release import inspect_seed, selected_resources


class ReleaseTests(unittest.TestCase):
    def test_resources_keep_selected_space_and_exclude_research_pointer_and_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = ['data/raw/observations/source.xlsx',
                     'data/derived/spatial/manifest.json',
                     'data/derived/spatial/grid-current.json',
                     'data/derived/spatial/versions/standard/units.geojson',
                     'data/derived/spatial/versions/selected/units.geojson',
                     'data/derived/spatial/versions/research/units.geojson']
            for name in paths:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}')
            write_json(root / 'data/derived/spatial/manifest.json', {'version': 'base'})
            write_json(root / 'data/derived/spatial/versions/standard/manifest.json', {'version': 'standard'})
            write_json(root / 'data/derived/spatial/versions/selected/manifest.json', {'version': 'selected'})
            write_json(root / 'data/derived/spatial/versions/research/manifest.json', {'version': 'research'})
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
                self.assertIn(paths[3], actual)
                self.assertIn(paths[4], actual)
                self.assertIn('slc_dem/slc_dem/w001001.adf',actual)
                self.assertIn('data/raw/spatial/source-package/subbasin_shp/four.shp',actual)
                self.assertIn(paths[2], actual)
                self.assertNotIn(paths[5], actual)
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
        self.assertIn('/DVerificationInstall=1',build)

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
