"""Exact member proportions, local publication history and report scheduling."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import unittest
import uuid
from unittest.mock import patch

from backend.database import Database
from backend import operational_store as store
from backend import warnings as warnings
from backend import reports


class WarningReportTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(__file__).resolve().parents[1]/'build'/('预警报告定向检查-'+uuid.uuid4().hex[:10])
        self.db = Database(self.folder)

    def test_highest_grade_uses_actual_member_proportions(self):
        rule = {'levels': [{'color':'blue','levelM':4543.7,'probability':.5},
                           {'color':'red','levelM':4543.9,'probability':.75}]}
        result = warnings.evaluate_rules(['01:00','02:00'], [[4543.8,4544.],[4543.6,4543.95]], rule)
        self.assertEqual(result['selected']['color'], 'red')
        self.assertEqual(result['selected']['firstTime'], '02:00')
        self.assertEqual(result['selected']['maximumProportion'], 1.)
        dry = warnings.evaluate_rules(['01:00'], [[4543.6]], {'levels':[{'color':'blue','levelM':4544.,'probability':0}]})
        self.assertIsNone(dry['selected'])

    def test_rule_source_required_and_grade_order_checked(self):
        with self.assertRaisesRegex(ValueError, '来源'):
            warnings.save_rules(self.db, {'mode':'business','levels':[{'color':'blue','levelM':4544.}]})
        with self.assertRaisesRegex(ValueError, '逐级'):
            warnings.save_rules(self.db, {'levels':[{'color':'blue','levelM':4544.}, {'color':'red','levelM':4543.}]})

    def test_publication_withdrawal_retains_history(self):
        rules = warnings.save_rules(self.db, {'levels':[{'color':'blue','levelM':4543.7,'probability':.5}]})
        forecast = {'id':'F-TEST','times':['2026-11-15T01:00:00'], 'memberCount':2,
                    'memberLakeLevels':[[4543.8],[4543.9]], 'businessEligible':False}
        with patch('backend.forecasts.detail', return_value=forecast):
            value = warnings.draft(self.db, {'forecastId':'F-TEST','ruleSetId':rules['id']})
        self.assertEqual(value['mode'], 'scenario')
        with self.assertRaisesRegex(ValueError, '发布人'):
            warnings.publish(self.db, value['id'], {})
        published = warnings.publish(self.db, value['id'], {'operator':'测试操作人'})
        self.assertEqual(published['status'], 'published')
        withdrawn = warnings.withdraw(self.db, value['id'], {'operator':'测试操作人','reason':'情景结束'})
        self.assertEqual([e['action'] for e in withdrawn['history']], ['draft','published','withdrawn'])
        with self.assertRaisesRegex(ValueError, '草稿'):
            warnings.publish(self.db, value['id'], {'operator':'测试操作人'})

    def test_sourced_business_rule_and_verified_curve_allow_formal_warning(self):
        rules = warnings.save_rules(self.db, {'mode':'business','source':'已批准湖位预警判据',
            'levels':[{'color':'blue','levelM':4543.7,'probability':.5}]})
        forecast = {'id':'F-BUSINESS','times':['2026-11-15T01:00:00'], 'memberCount':2,
            'memberLakeLevels':[[4543.8],[4543.9]], 'provisional':False,
            'lakeCurve':{'id':'CURVE-VERIFIED','provisional':False}}
        with patch('backend.forecasts.detail', return_value=forecast):
            value = warnings.draft(self.db, {'forecastId':forecast['id'],'ruleSetId':rules['id']})
        self.assertEqual(value['mode'], 'business')
        self.assertEqual(value['name'], '色林错漫溢风险IV级蓝色预警')
        self.assertEqual(value['status'], 'draft')
        published = warnings.publish(self.db, value['id'], {'operator':'业务确认人'})
        payload = reports._data(self.db, {'reportType':'warning','warningId':value['id']})
        self.assertIn('类别：业务预警', payload['sections'][0]['lines'])
        self.assertEqual(payload['warning']['ruleSnapshot']['source'], rules['source'])
        self.assertEqual(published['status'], 'published')
        withdrawn = warnings.withdraw(self.db, value['id'], {'operator':'业务确认人','reason':'更新预测'})
        self.assertEqual([r['action'] for r in withdrawn['history']], ['draft','published','withdrawn'])

    def test_business_rule_keeps_provisional_or_unknown_curve_as_scenario(self):
        rules = warnings.save_rules(self.db, {'mode':'business','source':'已批准湖位预警判据',
            'levels':[{'color':'blue','levelM':4543.7,'probability':.5}]})
        base = {'id':'F-SCENARIO','times':['2026-11-15T01:00:00'], 'memberCount':1,
            'memberLakeLevels':[[4543.8]]}
        for metadata in ({'provisional':True,'lakeCurve':{'provisional':False}},
                         {'provisional':False,'lakeCurve':{'provisional':True}},
                         {'lakeCurve':{'provisional':True}}, {}):
            with self.subTest(metadata=metadata), patch('backend.forecasts.detail', return_value={**base, **metadata}):
                value = warnings.draft(self.db, {'forecastId':base['id'],'ruleSetId':rules['id']})
            self.assertEqual(value['mode'], 'scenario')
            self.assertEqual(value['name'], '色林错漫溢风险IV级蓝色预警')

    def test_default_grades_combine_lake_level_and_breach_outflow(self):
        rules = warnings.default_rules()
        self.assertEqual([r['levelM'] for r in rules['levels']], [4544.3, 4544.45, 4544.55, 4544.65])
        self.assertEqual([r['outflowM3s'] for r in rules['levels']], [20., 40., 60., 80.])
        times = ['2026-11-15T01:00:00', '2026-11-15T02:00:00']
        cases = [([4544.10, 4544.12], [5, 6], None), ([4544.31, 4544.32], [5, 6], 'blue'),
                 ([4544.10, 4544.12], [30, 65], 'orange'), ([4544.50, 4544.66], [10, 12], 'red')]
        for levels, flows, expected in cases:
            with self.subTest(levels=levels, flows=flows):
                selected = warnings.evaluate_rules(times, [levels], rules, [flows])['selected']
                self.assertEqual(selected and selected['color'], expected)
        flow_only = warnings.evaluate_rules(times, [[4544.1, 4544.1]], rules, [[30, 65]])['selected']
        self.assertEqual((flow_only['basis'], flow_only['firstTime']), ('outflow', times[1]))

    def test_expiry_and_scheduled_reports(self):
        expired = store.save(self.db, 'warnings', {'status':'published','validUntil':'2020-01-01T00:00:00+08:00','history':[]}, prefix='WARNING-')
        warnings.expire(self.db)
        self.assertEqual(store.get(self.db, 'warnings', expired['id'])['status'], 'expired')
        tz = timezone(timedelta(hours=8))
        self.assertEqual(reports.due_reports(self.db, datetime(2026,11,16,7,59,tzinfo=tz)), [])
        due = reports.due_reports(self.db, datetime(2026,11,16,8,0,tzinfo=tz))
        self.assertEqual({r['reportType'] for r in due}, {'monitoring-daily','warning-weekly'})
        for request in due:
            store.save(self.db, 'reports', request, prefix='REPORT-')
        self.assertEqual(reports.due_reports(self.db, datetime(2026,11,16,8,10,tzinfo=tz)), [])

    def test_daily_report_pdf_and_json_do_not_invent_missing_observations(self):
        folder = self.folder/'tasks'/'REPORT-TEST'
        value = reports.run(self.db, {'reportType':'monitoring-daily','date':'2026-11-15'}, folder)
        self.assertEqual(value['rows'], [])
        payload = json.loads((folder/'监测日报.json').read_text(encoding='utf-8'))
        self.assertIn('暂无有效观测', payload['sections'][0]['lines'][0])
        self.assertTrue((folder/'监测日报.pdf').read_bytes().startswith(b'%PDF-'))
        self.assertGreater((folder/'监测日报.pdf').stat().st_size, 1000)
        from backend.maintenance import artifact_file
        task = {'id': folder.name, 'status': 'succeeded', 'result': value}
        self.assertEqual(artifact_file(self.db, task, '监测日报.pdf'), folder/'监测日报.pdf')


if __name__ == '__main__':
    unittest.main()
