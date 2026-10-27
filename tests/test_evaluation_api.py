"""Real localhost HTTP and worker queue integration, isolated tiny workspace."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from test_evaluation_archive import BASE,canonical,PROTOCOL
from backend.server import Platform
from backend.release_registry import current


class EvaluationApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=BASE)
        self.server=Platform(('127.0.0.1',0),self.temp.name)
        self.db=self.server.db;self.release=current(self.db)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.origin='http://127.0.0.1:'+str(self.server.server_port)
        for identifier,offset in [('BASE-api',1),('CAND-api',2)]:
            result={'modelFamily':'grid-continuous-v1','identity':{'tag':identifier},'observationProfile':'quality-v2',
                    'initialization':{'time':'2023-04-20T12:00:00','level':4544.03},'scheme':{'id':identifier},
                    'daily':[{'date':f'{year}-01-0{day}T00:00:00','observedLevel':day,'lakeLevel':day+offset,
                              'observedFlow':day,'duja':day+offset} for year in (2024,2025) for day in (1,2,3)]}
            with self.db.connect() as c:
                c.execute('INSERT INTO tasks(id,kind,status,created_at,updated_at,request,result) VALUES(?,?,?,?,?,?,?)',
                          (identifier,'simulation','succeeded','old','old','{}',canonical(result)))
        self.payload={'kind':'evaluation','name':'API archive fixture','clientRequestId':'api-one',
                      'protocolId':PROTOCOL['id'],'baselineReleaseId':self.release['id'],
                      'baselineResultId':'BASE-api','candidateResultId':'CAND-api'}

    def tearDown(self):
        self.server.shutdown();self.server.jobs.executor.shutdown(wait=True);self.server.server_close();self.thread.join(3)
        self.temp.cleanup()

    def api(self,path,payload=None):
        request=Request(self.origin+'/api/model/'+path,
                        canonical(payload).encode('utf-8') if payload is not None else None,
                        {'Content-Type':'application/json'} if payload is not None else {})
        with urlopen(request,timeout=10) as response:return json.load(response)

    def wait(self,identifier):
        limit=time.monotonic()+20
        while time.monotonic()<limit:
            task=self.api('tasks/'+identifier)
            if task['status'] not in ('queued','running','stopping'):return task
            time.sleep(.05)
        self.fail('tiny HTTP evaluation did not finish within20s')

    def test_real_queue_archive_download_compact_list_and_reuse(self):
        submitted=self.api('tasks',self.payload);finished=self.wait(submitted['id'])
        self.assertEqual(finished['status'],'succeeded',finished['message'])
        result=finished['result'];identifier=result['evaluationId']
        self.assertEqual(result['comparisonStatus'],'not-comparable')
        self.assertFalse(result['admission']['passed'])
        self.assertEqual(len(result['artifacts']),3)
        for artifact in result['artifacts']:
            with urlopen(self.origin+artifact['downloadUrl'],timeout=10) as response:raw=response.read()
            self.assertEqual(hashlib.sha256(raw).hexdigest(),artifact['sha256'])
            self.assertEqual(len(raw),artifact['bytes'])
        listed=self.api('evaluations')['evaluations']
        self.assertEqual(listed[0]['id'],identifier)
        record=self.api('evaluation?'+urlencode({'id':identifier}))
        if 'evaluation' in record:record=record['evaluation']
        self.assertEqual(record['years']['2025']['lake']['candidate']['rmse'],2)
        self.assertEqual(record['diagnostics']['initialStateStable'],'unknown')
        compact=next(t for t in self.api('tasks')['tasks'] if t['id']==finished['id'])
        self.assertEqual(compact['result']['evaluationId'],identifier)
        self.assertEqual(len(compact['result']['artifacts']),3)
        self.assertNotIn('daily',compact['result'])
        self.assertEqual(compact['termination'],'completed')
        again=self.api('tasks',self.payload)
        self.assertEqual(again['id'],finished['id'])
        second=self.wait(self.api('tasks',{**self.payload,'clientRequestId':'api-two'})['id'])
        self.assertEqual(second['status'],'succeeded',second['message'])
        self.assertEqual(second['result']['evaluationId'],identifier)
        self.assertTrue(second['result']['reused'])
        self.assertEqual(len(self.db.rows('SELECT id FROM evaluation_records')),1)
        self.assertEqual(current(self.db)['id'],self.release['id'])

    def test_forged_scores_checks_rejected_and_bad_input_never_archived(self):
        for field,value in [('scores',{'rmse':0}),('checks',{'initialStateStable':True})]:
            with self.assertRaises(HTTPError) as error:self.api('tasks',{**self.payload,field:value,'clientRequestId':field})
            self.assertEqual(error.exception.code,400)
            self.assertEqual(self.db.rows('SELECT id FROM evaluation_records'),[])
        bad=self.api('tasks',{**self.payload,'candidateResultId':'JOB-absent','clientRequestId':'bad'})
        failed=self.wait(bad['id'])
        self.assertEqual(failed['status'],'failed')
        self.assertEqual(self.db.rows('SELECT id FROM evaluation_records'),[])
        self.assertEqual(current(self.db)['id'],self.release['id'])
        with self.assertRaises(HTTPError) as error:self.api('evaluation?id=EVAL-absent')
        self.assertEqual(error.exception.code,404)


if __name__=='__main__':unittest.main(verbosity=2)
