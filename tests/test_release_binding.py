"""Small release fixtures: never copy real inputs or package a running workspace."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.common import canonical, digest, sha_file, write_json
from backend.database import Database
from backend.release_registry import current
from scripts.make_seed import build_seed, release_selection, seed_registry
from scripts.assemble_release import inspect_seed


class ReleaseBindingTests(unittest.TestCase):
    def published_resource(self, root, db, task, sources):
        """Real resource index/file verified by the backend helper, not a stub."""
        snapshot={'sources':sources,'flow':{},'level':{},'windows':[]}
        expected=digest(snapshot)
        task['result']['identity']['observationSHA256']=expected
        path=root/'config/operational/published-observation-snapshots/frozen.json'
        write_json(path,snapshot)
        write_json(root/'config/operational/published-observations.json',{
            'schemaVersion':1,'projectId':'selinco','snapshots':{task['id']:{
                'file':'published-observation-snapshots/frozen.json',
                'fileSHA256':sha_file(path),'observationSHA256':expected}}})
        release=current(db)
        row=db.rows('SELECT payload FROM scheme_revisions WHERE id=?',(release['schemeRevisionId'],))[0]
        revision=json.loads(row['payload']);revision['identity']=task['result']['identity']
        with db.connect() as connection:
            connection.execute('UPDATE scheme_revisions SET payload=? WHERE id=?',
                               (canonical(revision),release['schemeRevisionId']))
        return expected,path

    def extra_source(self, db, identifier):
        path=db.workspace/'observation-sources'/identifier/'metadata.json'
        path.parent.mkdir(parents=True);path.write_bytes(('source-'+identifier).encode())
        with db.connect() as connection:
            connection.execute('INSERT INTO sources VALUES(?,?,?,?,?,?)',(
                identifier,identifier+'.json',sha_file(path),path.stat().st_size,'2026',
                canonical({'archivedPath':path.relative_to(db.workspace).as_posix()})))
        return {'id':identifier,'sha256':sha_file(path)},path

    def fixture(self, folder):
        db = Database(folder)
        manifests = {}
        for identifier in ('frozen','latest'):
            path = db.workspace/'inputs/products'/identifier/'forcing.npz'
            path.parent.mkdir(parents=True)
            path.write_bytes(('small-fixture-'+identifier).encode())
            manifest={'data':'forcing.npz','dataSHA256':sha_file(path),'spatialVersion':'space','end':'2026-01-01' if identifier=='frozen' else '2027-01-01'}
            manifests[identifier]=manifest
            write_json(path.parent/'manifest.json',manifest)
            with db.connect() as connection:
                connection.execute('INSERT INTO input_products VALUES(?,?,?,?)',(identifier,identifier,'2026',canonical(manifest)))
        raw=db.workspace/'observation-sources/raw/value.xlsx'
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b'observation-source-fixture')
        with db.connect() as connection:
            connection.execute('INSERT OR IGNORE INTO stations VALUES(?,?,?,?,?,?)',('SLC','main','lake',89,32,'{}'))
            connection.execute('INSERT INTO sources VALUES(?,?,?,?,?,?)',('source','value.xlsx',sha_file(raw),raw.stat().st_size,'2026',canonical({'archivedPath':'observation-sources/raw/value.xlsx'})))
            connection.execute('INSERT INTO observations(source_id,station_id,variable,time,step,unit,value,raw_text,quality,sheet,cell) VALUES(?,?,?,?,?,?,?,?,?,?,?)',('source','SLC','level','2025-01-01','day','m',4544,'4544','valid','sheet','A1'))
            connection.execute('INSERT INTO tasks(id,kind,status,created_at,updated_at,request) VALUES(?,?,?,?,?,?)',('research','trial','succeeded','2026','2026','{}'))
        downloads=db.workspace/'downloads/era5-land/unrelated.nc'
        downloads.parent.mkdir(parents=True)
        downloads.write_bytes(b'not-for-release')
        identity={'inputId':'frozen','inputSHA256':manifests['frozen']['dataSHA256'],'spatialVersion':'space'}
        result={'inputId':'frozen','identity':identity,'scheme':{'id':'standard','revision':2,'spatialVersion':'space','initialization':{'lakeLevel':'window'}},'parameters':{'KC':1},'partitionParameters':{}}
        task={'id':'builtin','result':result}
        release={'id':'release','resultId':'builtin','status':'protected-legacy','admissionPassed':False}
        seed_registry(db,db,release,task)
        item={'id':'builtin','directory':'chosen','files':{'result.json':{'sha256':'fixture','bytes':1}}}
        return db,task,item

    def test_frozen_result_input_wins_over_newer_catalog_and_source_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db,task,item=self.fixture(Path(tmp)/'source')
            before=db.path.read_bytes()
            with patch('backend.published_results.catalog',return_value=[item]),patch('backend.published_results.detail',return_value=task),patch('backend.operational_scheme.catalog',side_effect=AssertionError('latest catalog must not be used')):
                selected=release_selection(db)
            self.assertEqual(selected[3]['id'],'frozen')
            self.assertEqual(before,db.path.read_bytes())

    def test_clean_seed_keeps_registry_sources_and_frozen_input_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db,task,item=self.fixture(Path(tmp)/'source')
            with patch('backend.published_results.catalog',return_value=[item]),patch('backend.published_results.detail',return_value=task):
                output=build_seed(db.workspace,Path(tmp)/'seed',standard_only=True)
                counts,scheme=inspect_seed(output)
            self.assertEqual(counts['tasks'],0)
            self.assertEqual(counts['states'],0)
            self.assertEqual(counts['input_products'],1)
            self.assertEqual(counts['observations'],1)
            self.assertEqual(scheme['inputId'],'frozen')
            self.assertEqual(scheme['release']['id'],'release')
            self.assertFalse((output/'downloads').exists())
            self.assertFalse((output/'tasks').exists())
            self.assertFalse((output/'inputs/products/latest').exists())
            self.assertTrue((output/'observation-sources/raw/value.xlsx').is_file())
            report=json.loads((output/'seed-report.json').read_text(encoding='utf-8'))
            self.assertFalse(report['release']['admissionPassed'])
            self.assertEqual(report['scientificStatus'],'protected-legacy')
            self.assertEqual(report['resultIdentity'],task['result']['identity'])

    def test_missing_frozen_input_rejects_instead_of_falling_back_to_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            db,task,item=self.fixture(tmp)
            with db.connect() as connection:
                connection.execute('DELETE FROM input_products WHERE id=?',('frozen',))
            with patch('backend.published_results.catalog',return_value=[item]),patch('backend.published_results.detail',return_value=task),self.assertRaisesRegex(ValueError,'冻结输入'):
                release_selection(db)

    def test_tampered_input_and_missing_source_reject_release_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db,task,item=self.fixture(Path(tmp)/'source')
            (db.workspace/'inputs/products/frozen/forcing.npz').write_bytes(b'tampered')
            with patch('backend.published_results.catalog',return_value=[item]),patch('backend.published_results.detail',return_value=task),self.assertRaisesRegex(ValueError,'摘要'):
                build_seed(db.workspace,Path(tmp)/'bad-seed')
            raw=db.workspace/'observation-sources/raw/value.xlsx'
            raw.write_bytes(b'tampered-source')
            with patch('backend.published_results.catalog',return_value=[item]),patch('backend.published_results.detail',return_value=task),self.assertRaisesRegex(ValueError,'观测来源'):
                build_seed(db.workspace,Path(tmp)/'bad-source-seed')

    def test_nonempty_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            db,task,item=self.fixture(Path(tmp)/'source')
            output=Path(tmp)/'seed';output.mkdir();marker=output/'keep.txt';marker.write_text('keep')
            with self.assertRaisesRegex(ValueError,'非空'):
                build_seed(db.workspace,output)
            self.assertEqual(marker.read_text(),'keep')

    def test_wrapped_results_select_their_own_spatial_version(self):
        from scripts.assemble_release import selected_resources
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for version in ('standard','selected','research'):
                target=root/'data/derived/spatial/versions'/version/'units.geojson'
                target.parent.mkdir(parents=True);target.write_text('{}')
            write_json(root/'data/derived/spatial/manifest.json',{'version':'standard'})
            write_json(root/'data/final-results/chosen/result.json',{'result':{'spatialVersion':'selected'}})
            with patch('scripts.assemble_release.ROOT',root):
                paths={str(p.relative_to(root)).replace('\\','/') for p in selected_resources({'spatialVersion':'standard'},[{'directory':'chosen'}])}
            self.assertIn('data/derived/spatial/versions/selected/units.geojson',paths)
            self.assertNotIn('data/derived/spatial/versions/research/units.geojson',paths)

    def test_frozen_metadata_source_without_observations_is_kept_and_unrelated_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);db,task,item=self.fixture(root/'source')
            required,raw=self.extra_source(db,'frozen-metadata')
            unrelated,other=self.extra_source(db,'unrelated-research')
            observed=db.rows('SELECT id,sha256 FROM sources WHERE id=?',('source',))[0]
            expected,_=self.published_resource(root,db,task,[observed,required])
            before=db.path.read_bytes()
            with patch('backend.published_results.catalog',return_value=[item]),patch('backend.published_results.detail',return_value=task),patch('backend.release_runtime.ROOT',root):
                output=build_seed(db.workspace,root/'seed')
                counts,_=inspect_seed(output)
            seed=Database(output)
            self.assertEqual({row['id'] for row in seed.rows('SELECT id FROM sources')},{'source','frozen-metadata'})
            self.assertEqual(counts['observations'],1)
            self.assertTrue((output/raw.relative_to(db.workspace)).is_file())
            self.assertEqual(sha_file(output/raw.relative_to(db.workspace)),required['sha256'])
            self.assertFalse((output/other.relative_to(db.workspace)).exists())
            self.assertEqual(before,db.path.read_bytes())
            report=json.loads((output/'seed-report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['frozenObservationSHA256'],expected)
            self.assertEqual(report['frozenObservationRequiredSourceCount'],2)
            self.assertEqual(len(report['observationSources']),2)

    def test_missing_frozen_source_registration_or_changed_sha_rejects_seed(self):
        for problem in ('missing-row','different-row-sha','missing-file','changed-file'):
            with self.subTest(problem=problem),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);db,task,item=self.fixture(root/'source')
                required,raw=self.extra_source(db,'frozen-metadata')
                self.published_resource(root,db,task,[required])
                if problem in ('missing-row','different-row-sha'):
                    with db.connect() as connection:
                        if problem=='missing-row':connection.execute('DELETE FROM sources WHERE id=?',(required['id'],))
                        else:connection.execute('UPDATE sources SET sha256=? WHERE id=?',('0'*64,required['id']))
                elif problem=='missing-file':raw.rename(raw.with_name('retained-moved-metadata.json'))
                else:raw.write_bytes(b'changed-frozen-source')
                with patch('backend.published_results.catalog',return_value=[item]),patch('backend.published_results.detail',return_value=task),patch('backend.release_runtime.ROOT',root),self.assertRaisesRegex(ValueError,'观测来源'):
                    build_seed(db.workspace,root/'seed')

    def test_tampered_frozen_snapshot_resource_rejects_before_source_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);db,task,item=self.fixture(root/'source')
            required,raw=self.extra_source(db,'frozen-metadata')
            _,resource=self.published_resource(root,db,task,[required])
            resource.write_bytes(b'changed-snapshot')
            with patch('backend.published_results.catalog',return_value=[item]),patch('backend.published_results.detail',return_value=task),patch('backend.release_runtime.ROOT',root),self.assertRaisesRegex(ValueError,'冻结观测文件摘要'):
                build_seed(db.workspace,root/'seed')
            self.assertFalse((root/'seed').exists())

    def test_old_fixture_without_snapshot_resource_keeps_original_source_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);db,task,item=self.fixture(root/'source')
            self.extra_source(db,'unrelated-research')
            with patch('backend.published_results.catalog',return_value=[item]),patch('backend.published_results.detail',return_value=task),patch('backend.release_runtime.ROOT',root):
                output=build_seed(db.workspace,root/'seed')
            report=json.loads((output/'seed-report.json').read_text(encoding='utf-8'))
            self.assertIsNone(report['frozenObservationSHA256'])
            self.assertEqual(report['frozenObservationRequiredSourceCount'],0)
            self.assertEqual({row['sourceId'] for row in report['observationSources']},{'source'})

    def test_inspection_rejects_seed_missing_required_frozen_source_or_report_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);db,task,item=self.fixture(root/'source')
            required,_=self.extra_source(db,'frozen-metadata')
            self.published_resource(root,db,task,[required])
            with patch('backend.published_results.catalog',return_value=[item]),patch('backend.published_results.detail',return_value=task),patch('backend.release_runtime.ROOT',root):
                output=build_seed(db.workspace,root/'seed')
                self.assertEqual(inspect_seed(output)[0]['observations'],1)
                report_path=output/'seed-report.json'
                report=json.loads(report_path.read_text(encoding='utf-8'))
                for field,value in [('frozenObservationSHA256','0'*64),('frozenObservationRequiredSourceCount',0),('frozenObservationRequiredSourceCount',True)]:
                    with self.subTest(field=field,value=value):
                        write_json(report_path,{**report,field:value})
                        with self.assertRaisesRegex(ValueError,'冻结观测摘要或必需来源数量'):
                            inspect_seed(output)
                write_json(report_path,report)
                seed=Database(output)
                with seed.connect() as connection:
                    connection.execute('UPDATE sources SET sha256=? WHERE id=?',('0'*64,required['id']))
                with self.assertRaisesRegex(ValueError,'冻结观测必需来源'):
                    inspect_seed(output)
                with seed.connect() as connection:
                    connection.execute('DELETE FROM sources WHERE id=?',(required['id'],))
                with self.assertRaisesRegex(ValueError,'冻结观测必需来源'):
                    inspect_seed(output)


if __name__=='__main__':
    unittest.main()
