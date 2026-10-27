import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.common import ROOT, VERSION, write_json, sha_file
from backend.database import Database
from scripts.assemble_release import inspect_seed, selected_resources


class ReleaseTests(unittest.TestCase):
    def test_resources_keep_selected_space_and_exclude_research_pointer_and_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = ['data/raw/observations/source.xlsx',
                     'data/derived/spatial/display.geojson',
                     'data/derived/spatial/grid-current.json',
                     'data/derived/spatial/versions/standard/units.geojson',
                     'data/derived/spatial/versions/selected/units.geojson',
                     'data/derived/spatial/versions/research/units.geojson']
            for name in paths:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}')
            write_json(root / 'data/derived/spatial/manifest.json', {'version': 'standard'})
            write_json(root / 'data/final-results/chosen/result.json', {'spatialVersion': 'selected'})
            with patch('scripts.assemble_release.ROOT', root):
                actual = {p.relative_to(root).as_posix() for p in selected_resources(
                    {'spatialVersion': 'standard'}, [{'directory': 'chosen'}])}
                self.assertNotIn(paths[0], actual)  # referenced observation originals live in the seed
                self.assertIn(paths[3], actual)
                self.assertIn(paths[4], actual)
                self.assertNotIn(paths[2], actual)
                self.assertNotIn(paths[5], actual)
                with self.assertRaises(ValueError):
                    selected_resources({'spatialVersion': 'research'}, [])

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
