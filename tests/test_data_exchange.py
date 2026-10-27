import tempfile
import unittest
from pathlib import Path

from backend.database import Database
from backend.data_exchange import import_records, import_file, preview_file
from backend.observations import daily_series


class DataExchangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(self.tmp.name)
        with self.db.connect() as c:
            c.execute('INSERT INTO stations VALUES(?,?,?,?,?,?)', ('SLC','lake','lake',89.,32.,'{}'))

    def tearDown(self):
        self.tmp.cleanup()

    def document(self, records=None):
        return {'schemaVersion':1,'projectId':'selinco','sourceKey':'lake-daily',
                'timeZone':'Asia/Shanghai', 'records':records or [
                    {'station':'SLC','variable':'level','time':'2024-06-01','step':'day','value':4544.2,'unit':'m'}]}

    def test_duplicate_revisions_and_old_file_reimport_do_not_roll_back(self):
        doc = self.document()
        first = import_records(self.db, doc)
        self.assertEqual(import_records(self.db, doc)['status'],'unchanged')
        revised = self.document([{**doc['records'][0], 'value':4544.3}])
        second = import_records(self.db,revised)
        self.assertNotEqual(first['sourceId'],second['sourceId'])
        self.assertFalse(import_records(self.db,doc)['active'])
        self.assertAlmostEqual(daily_series(self.db,'SLC','level').iloc[0],4544.3)
        self.assertEqual(self.db.summary()['counts']['observations'],2)

    def test_validation_is_atomic_and_does_not_create_unknown_stations(self):
        original = self.document()['records'][0]
        for bad in ({'station':'other'}, {'unit':'unknown'}, {'value':float('nan')},
                    {'step':'day','periodEnd':'2024-06-03'}, {'variable':'flow','value':-1},
                    {'step':'hour','time':'2024-06-01T00:15:00'}):
            with self.assertRaises(ValueError):
                import_records(self.db,self.document([original,{**original,**bad}]))
        self.assertEqual(self.db.summary()['counts']['sources'],0)
        self.assertEqual(self.db.summary()['counts']['observations'],0)

    def test_units_timezone_minutes_and_stopped_marks(self):
        rows = [
            {'station':'SLC','variable':'level','time':'2024-06-01T00:17:00Z','step':'irregular','value':454420,'unit':'cm'},
            {'station':'SLC','variable':'temperature','time':'2024-06-01T01:00:00Z','step':'hour','value':273.15,'unit':'K'},
            {'station':'SLC','variable':'level','time':'2024-06-02T00:00:00','step':'irregular','value':'L','quality':'ice_or_stopped','unit':'m'}]
        import_records(self.db,self.document(rows))
        level = self.db.observations('SLC','level')
        self.assertEqual(level[0]['time'],'2024-06-01T08:17:00')
        self.assertAlmostEqual(level[0]['value'],4544.2)
        self.assertEqual(level[1]['raw_text'],'L')
        self.assertIsNone(level[1]['value'])
        self.assertEqual(self.db.observations('SLC','temperature')[0]['value'],0)

    def test_missing_intervals_are_not_zero_rain_and_duplicate_times_rejected(self):
        r={'station':'SLC','variable':'precipitation','time':'2024-06-01T12:00:00','step':'irregular','value':.001,'unit':'m',
           'periodStart':'2024-06-01T11:00:00','periodEnd':'2024-06-01T12:00:00'}
        with self.assertRaises(ValueError):import_records(self.db,self.document([r,r]))
        with self.assertRaises(ValueError):import_records(self.db,self.document([{k:v for k,v in r.items() if k not in ('periodStart','periodEnd')}]))
        import_records(self.db,self.document([r]))
        self.assertEqual(list(daily_series(self.db,'SLC','precipitation').values),[1.])
        self.assertFalse(self.db.summary()['coverage'][0]['metadata']['zeroForAbsent'])

    def test_export_mapping_and_file_archive(self):
        from openpyxl import Workbook
        path = Path(self.tmp.name)/'incoming/export.xlsx'
        path.parent.mkdir()
        book=Workbook();book.active.title='data';book.active.append(['时间','水位']);book.active.append(['2024-06-01',4544.2]);book.save(path)
        request={**self.document(),'filename':'export.xlsx','sheet':'data',
                 'mapping':{'time':'时间','value':'水位'},
                 'defaults':{'station':'SLC','variable':'level','unit':'m','step':'day'}}
        result=import_file(self.db,request)
        self.assertEqual(result['records'],1)
        self.assertTrue((Path(self.tmp.name)/'observation-sources'/result['sourceId']/'export.xlsx').is_file())
        with self.assertRaises(ValueError):import_file(self.db,{**request,'filename':'../outside.xlsx'})

    def test_incremental_merge_and_explicit_conflict_policy(self):
        base = self.document()['records'][0]
        first = import_records(self.db, self.document([base]))
        later = {**base, 'time': '2024-06-02', 'value': 4544.4}
        merged = import_records(self.db, {**self.document([later]), 'mode': 'append'})
        self.assertNotEqual(first['sourceId'], merged['sourceId'])
        self.assertEqual([r['value'] for r in self.db.observations('SLC', 'level')], [4544.2, 4544.4])
        changed = {**base, 'value': 4544.9}
        with self.assertRaisesRegex(ValueError, '冲突'):
            import_records(self.db, {**self.document([changed]), 'mode': 'append'})
        self.assertEqual([r['value'] for r in self.db.observations('SLC', 'level')], [4544.2, 4544.4])
        kept = import_records(self.db, {**self.document([changed]), 'mode': 'append', 'duplicatePolicy': 'keep'})
        self.assertEqual(kept['status'], 'unchanged')
        replaced = import_records(self.db, {**self.document([changed]), 'mode': 'append', 'duplicatePolicy': 'replace'})
        self.assertEqual((replaced['duplicates'], replaced['conflicts']), (1, 1))
        self.assertEqual([r['value'] for r in self.db.observations('SLC', 'level')], [4544.9, 4544.4])
        self.assertEqual(self.db.rows('SELECT COUNT(*) n FROM sources')[0]['n'], 3)

    def test_file_preview_does_not_import_and_reports_conflicts(self):
        from openpyxl import Workbook
        import_records(self.db, self.document())
        path = Path(self.tmp.name) / 'incoming/preview.xlsx'
        path.parent.mkdir()
        book = Workbook(); book.active.title = '观测'; book.active.append(['时间', '水位'])
        book.active.append(['2024-06-01', 4544.4]); book.active.append(['2024-06-01', 4544.5])
        book.active.append(['2024-06-02', '错误值']); book.save(path)
        request = {'filename': 'preview.xlsx', 'sourceKey': 'lake-daily',
                   'mapping': {'time': '时间', 'value': '水位'},
                   'defaults': {'station': 'SLC', 'variable': 'level', 'unit': 'm', 'step': 'day'}}
        preview = preview_file(self.db, request)
        self.assertEqual(preview['sheets'], ['观测'])
        self.assertEqual(preview['suggestedMapping']['time'], '时间')
        self.assertEqual((preview['records'], preview['valid'], preview['invalid']), (3, 2, 1))
        self.assertEqual((preview['duplicates'], preview['existingDuplicates'], preview['conflicts']), (1, 2, 2))
        self.assertEqual(preview['issues'][0]['row'], 4)
        self.assertEqual(self.db.rows('SELECT COUNT(*) n FROM sources')[0]['n'], 1)

    def test_end_hour_midnight_and_incomplete_days(self):
        import pandas as pd
        rows=[{'station':'SLC','variable':'precipitation','time':str(t),'step':'hour','value':1,'unit':'mm'}
              for t in pd.date_range('2024-06-01T01:00',periods=25,freq='h')]
        import_records(self.db,self.document(rows))
        daily=daily_series(self.db,'SLC','precipitation')
        self.assertEqual(daily.loc['2024-06-01'],24.)
        self.assertTrue(pd.isna(daily.loc['2024-06-02']))


if __name__=='__main__':unittest.main()
