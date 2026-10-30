import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from collections import Counter

from openpyxl import Workbook, load_workbook

from backend.database import Database
from backend.native_observation_import import preview, commit
from backend.observations import daily_series


class NativeObservationImportTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix='selinco-native-test-'))
        self.db = Database(self.workspace)
        with self.db.connect() as conn:
            for sid in ('SLC', 'SLC-E', 'SLC-S', 'SLC-W', 'SLC-N', 'DUJA'):
                conn.execute('INSERT INTO stations VALUES(?,?,?,?,?,?)', (sid, sid, 'lake', None, None, '{}'))
        (self.workspace / 'incoming').mkdir()

    def workbook(self, name, rows, sheets=None):
        path = self.workspace / 'incoming' / name
        book = Workbook()
        sheet = book.active
        sheet.title = sheets[0] if sheets else 'Sheet1'
        for row in rows:
            sheet.append(row)
        for title in (sheets or [])[1:]:
            sheet = book.create_sheet(title)
            for row in rows:
                sheet.append(row)
        book.save(path)
        return {'filename': name, 'originalName': name}

    def send(self, files):
        request = {'files': files}
        receipt = preview(self.db, request)
        return receipt, commit(self.db, {**request, 'previewToken': receipt['previewToken']})

    def test_same_name_increment_does_not_drop_prior_dates_and_preserves_changes(self):
        first = self.workbook('2025_色林错逐时水位.xlsx', [[datetime(2025, 5, 1), 4544.2],
                                                       [datetime(2025, 5, 2), 4544.3]])
        before, saved = self.send([first])
        self.assertEqual((before['counts']['new'], saved['new']), (2, 2))
        prior_id = saved['sources'][0]
        self.assertTrue((self.workspace / 'observation-sources' / prior_id / first['originalName']).is_file())
        second = self.workbook('2025_色林错逐时水位.xlsx', [[datetime(2025, 5, 2), 4544.4],
                                                        [datetime(2025, 5, 3), 4544.5]])
        before, saved = self.send([second])
        self.assertEqual((before['counts']['new'], before['counts']['changed']), (1, 1))
        self.assertEqual([r['value'] for r in self.db.observations('SLC', 'level')], [4544.2, 4544.4, 4544.5])
        self.assertEqual(self.db.rows('SELECT COUNT(*) n FROM observations')[0]['n'], 4)
        before, saved = self.send([second])
        self.assertEqual((before['counts']['same'], saved['status']), (2, 'unchanged'))

    def test_four_shore_and_unconfirmed_rain_coverage(self):
        shore = self.workbook('2026_色林错东南西北逐时水位.xlsx', [[datetime(2026, 5, 1), 4544.2]],
                              ['东岸', '南岸', '西岸', '北岸'])
        rain = self.workbook('2026_杜加逐时雨量.xlsx', [[datetime(2026, 5, 1, 12), 0.5]])
        info, _ = self.send([shore, rain])
        self.assertEqual({r['station'] for r in info['summary']}, {'SLC-E', 'SLC-S', 'SLC-W', 'SLC-N', 'DUJA'})
        coverage = self.db.summary()['coverage']
        self.assertTrue(all(not r['metadata']['coverageConfirmed'] and not r['metadata']['zeroForAbsent'] for r in coverage))

    def test_shore_sheets_identified_without_direction_or_variable_in_filename(self):
        shore = self.workbook('系统导出.xlsx', [[datetime(2026, 5, 1), 4544.2]],
                              ['色南', '色北', '色东', '色西', '说明'])
        report, saved = self.send([shore])
        self.assertEqual({r['station'] for r in report['summary']},
                         {'SLC-E', 'SLC-S', 'SLC-W', 'SLC-N'})
        self.assertEqual(saved['new'], 4)
        self.assertTrue(all(r['variable'] == 'level' for r in report['summary']))

    def test_actual_four_shore_workbooks_match_raw_sheet_rows(self):
        import shutil
        from backend.native_observation_import import _parse
        folder = Path('data/raw/observations/slc/slc/水位')
        fixtures = [
            ('2025_色林错东南西北逐时水位.xlsx',
             {'25东': 'SLC-E', '25南': 'SLC-S', '25西': 'SLC-W', '25北': 'SLC-N'}),
            ('2026_色林错东南西北逐时水位.xlsx',
             {'色东': 'SLC-E', '色南': 'SLC-S', '色西': 'SLC-W', '色北': 'SLC-N'}),
        ]
        if not all((folder / name).is_file() for name, _ in fixtures):
            self.skipTest('本机四岸原始工作簿未安装')
        for name, stations in fixtures:
            with self.subTest(file=name):
                path = self.workspace / 'incoming' / ('token-' + name)
                shutil.copyfile(folder / name, path)
                item = {'filename': path.name, 'originalName': '系统导出.xlsx'}
                rows = _parse(self.db, item)[-1]
                workbook = load_workbook(path, read_only=True, data_only=True)
                try:
                    raw = [(sheet.title, stations[sheet.title], values[0].isoformat(timespec='seconds'), str(values[-1]))
                           for sheet in workbook for values in sheet.iter_rows(values_only=True)
                           if values and isinstance(values[0], datetime)]
                finally:
                    workbook.close()
                actual = [(r['sheet'], r['station'], r['time'], r['raw']) for r in rows]
                self.assertEqual(actual, raw)
                self.assertEqual(set(Counter(r['station'] for r in rows)), set(stations.values()))
                report, saved = self.send([item])
                self.assertEqual(report['counts']['new'], len(raw))
                self.assertEqual(saved['new'], len(raw))
                self.assertEqual({r['station'] for r in report['summary']}, set(stations.values()))

    def test_xls_year_uses_original_filename_and_labelled_report_year(self):
        import shutil
        original = Path('data/raw/observations/slc/dj/流量/90306953-2025年杜加站逐日平均流量表.xls')
        if not original.is_file():
            self.skipTest('本机原始流量年报表未安装')
        # The upload token deliberately contains a different plausible year.
        path = self.workspace / 'incoming' / ('abc2020abc-' + original.name)
        shutil.copyfile(original, path)
        info = preview(self.db, {'files': [{'filename': path.name, 'originalName': original.name}]})
        self.assertEqual(info['summary'][0]['start'][:4], '2025')
        self.assertEqual(info['summary'][0]['end'][:4], '2025')
        # Original name need not contain year, station or variable if the
        # actual annual report explicitly supplies them in its header.
        info = preview(self.db, {'files': [{'filename': path.name, 'originalName': '系统导出.xls'}]})
        self.assertEqual((info['summary'][0]['station'], info['summary'][0]['variable']), ('DUJA', 'flow'))
        self.assertEqual(info['summary'][0]['start'][:4], '2025')
        with self.assertRaisesRegex(ValueError, '年度.*不一致'):
            preview(self.db, {'files': [{'filename': path.name, 'originalName': '2026_杜加流量.xls'}]})

    def test_batch_identical_overlap_is_deduplicated(self):
        one = self.workbook('2025_色林错逐时水位.xlsx', [[datetime(2025, 5, 1), 4544.2]])
        two = self.workbook('副本_2025_色林错逐时水位.xlsx', [[datetime(2025, 5, 1), 4544.2]])
        receipt, saved = self.send([one, two])
        self.assertEqual((receipt['counts']['new'], saved['new']), (1, 1))
        self.assertEqual(len(self.db.observations('SLC', 'level')), 1)

    def test_event_increment_updates_existing_confirmed_coverage(self):
        from backend.common import canonical, now
        with self.db.connect() as conn:
            conn.execute('INSERT INTO sources VALUES(?,?,?,?,?,?)', ('SRC-original', 'original', 'original-sha', 1, now(), '{}'))
            conn.execute('INSERT INTO observations(source_id,station_id,variable,time,period_start,period_end,step,unit,value,raw_text,quality,sheet,cell) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('SRC-original','DUJA','precipitation','2026-05-01T12:00:00',None,'2026-05-01T12:00:00','irregular','mm',1,'1','valid','Sheet1','R1C2'))
            conn.execute('INSERT INTO coverage VALUES(?,?,?,?,?,?,?,?,?)', ('SRC-original','DUJA','precipitation','irregular','2026-05-01T00:00:00','2026-05-03T00:00:00',1,1,canonical({'coverageConfirmed':True,'zeroForAbsent':True})))
        rain = self.workbook('2026_杜加逐时雨量.xlsx', [[datetime(2026, 5, 1, 12), 2.0], [datetime(2026, 5, 2, 13), 3.0]])
        self.send([rain])
        series = daily_series(self.db, 'DUJA', 'precipitation')
        self.assertEqual(series.tolist(), [2.0, 3.0])

    def test_preview_token_rejects_change_between_check_and_write(self):
        file = self.workbook('2026_色林错逐时水位.xlsx', [[datetime(2026, 5, 1), 4544.2]])
        request = {'files': [file]}
        token = preview(self.db, request)['previewToken']
        self.workbook(file['filename'], [[datetime(2026, 5, 1), 4544.3]])
        with self.assertRaisesRegex(ValueError, '重新检查'):
            commit(self.db, {**request, 'previewToken': token})
        self.assertEqual(self.db.summary()['counts']['sources'], 0)

    def test_incremental_blank_keeps_existing_value_but_ice_marker_updates(self):
        first = self.workbook('2025_色林错逐时水位.xlsx', [[datetime(2025, 5, 1), 4544.2]])
        self.send([first])
        blank = self.workbook('2025_色林错逐时水位.xlsx', [[datetime(2025, 5, 1), None]])
        receipt, saved = self.send([blank])
        self.assertEqual(receipt['counts']['preserved'], 1)
        self.assertEqual(saved['status'], 'unchanged')
        self.assertEqual(self.db.observations('SLC', 'level')[0]['value'], 4544.2)
        stopped = self.workbook('2025_色林错逐时水位.xlsx', [[datetime(2025, 5, 1), 'L']])
        receipt, saved = self.send([stopped])
        self.assertEqual(receipt['counts']['changed'], 1)
        self.assertEqual(self.db.observations('SLC', 'level')[0]['quality'], 'ice_or_stopped')

    def test_original_xls_yearly_rain_and_incremental_blank_preserves_value(self):
        import shutil
        original = Path('data/raw/observations/slc/blq/2025_巴勒切雨量报表.xls')
        if not original.is_file():
            self.skipTest('本机原始雨量表未安装')
        path = self.workspace / 'incoming' / original.name
        shutil.copyfile(original, path)
        item = {'filename': original.name, 'originalName': original.name}
        with self.db.connect() as conn:
            conn.execute('INSERT INTO stations VALUES(?,?,?,?,?,?)', ('BLQ','巴勒切','rain',None,None,'{}'))
        report = preview(self.db, {'files': [item]})
        self.assertEqual(report['summary'][0]['records'], 365)
        self.assertEqual((report['summary'][0]['station'], report['summary'][0]['step']), ('BLQ', 'day'))


if __name__ == '__main__':
    unittest.main()
