import test from 'node:test'
import assert from 'node:assert/strict'
import {isResultRecord,resultDisplayPeriod,displayCoverageDates,displayAnnualMetrics} from '../frontend/src/result-catalog.mjs'

test('the result catalogue excludes data and export tasks',()=>{
 for(const kind of ['simulation','continuation','trial','calibration','two-stage-calibration','calibration-replay']){
  assert.equal(isResultRecord({kind,status:'succeeded'}),true,kind)
 }
 for(const kind of ['update-forcing','import-observations','export-result','evaluation','backup']){
  assert.equal(isResultRecord({kind,status:'succeeded'}),false,kind)
 }
 assert.equal(isResultRecord({builtin:true}),true)
 assert.equal(isResultRecord({kind:'calibration-workflow',status:'failed'}),true)
 assert.equal(isResultRecord({kind:'sensitivity',status:'succeeded'}),false)
})

test('result table uses the selected result display coverage rather than its warmup',()=>{
 const task={id:'official',request:{start:'2017-01-01T00:00:00',end:'2026-10-21T00:00:00'}}
 const detail={id:'official',result:{displayCoverage:{start:'2021-04-23T00:00:00',endExclusive:'2026-10-21T00:00:00'}}}
 assert.equal(resultDisplayPeriod(task,detail),'2021-04-23 — 2026-10-20')
 assert.equal(resultDisplayPeriod(task,{...detail,id:'another'}),'2017-01-01 — 2026-10-20')
})

test('annual display and end date follow the stored coverage without discarding measured years',()=>{
 const coverage={start:'2021-04-23T00:00:00',endExclusive:'2026-10-21T00:00:00'}
 assert.deepEqual(displayCoverageDates(coverage),{start:'2021-04-23',end:'2026-10-20'})
 assert.deepEqual(Object.keys(displayAnnualMetrics({2017:{},2020:{},2021:{},2025:{},2026:{}},coverage)),['2021','2025','2026'])
 assert.deepEqual(displayCoverageDates({start:'2024-01-01',end:'2024-12-31'}),{start:'2024-01-01',end:'2024-12-31'})
})
