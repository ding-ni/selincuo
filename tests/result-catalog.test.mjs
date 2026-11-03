import test from 'node:test'
import assert from 'node:assert/strict'
import {isResultRecord,resultDisplayPeriod,displayCoverageDates,displayAnnualMetrics,calculationProcess} from '../frontend/src/result-catalog.mjs'

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

test('calculation details reflect the frozen request and actual search record',()=>{
 const row=calculationProcess({
  request:{start:'2017-01-01',periods:{warmupStart:'2017-01-01'},twoStage:{seeds:[42,142,242]},
   search:{bounds:{WM:[40,300]},keys:['WM'],seeds:[42,142,242]},parameters:{WM:120}},
  result:{modelFamily:'grid-continuous-v1',start:'2017-01-01',end:'2026-10-21',inputId:'INPUT-best',
   spatialVersion:'AGG-best',parameters:{WM:156},calibration:{evaluations:72609,converged:false,
    stages:[{stage:'分期率定与检验',evaluations:35972,termination:'budget-exhausted'}]}}
 })
 assert.equal(row.warmupStart,'2017-01-01')
 assert.deepEqual(row.seeds,[42,142,242])
 assert.equal(row.evaluations,72609)
 assert.equal(row.parameters[0].initial,120)
 assert.deepEqual(row.parameters[0].bounds,[40,300])
 assert.equal(row.parameters[0].value,156)
 assert.equal(row.stages[0].evaluations,35972)
})


test('parameters reflect water calibration, lake calibration and fixed terms',()=>{
 const record={request:{twoStage:{mode:'both'},search:{keys:['WM'],bounds:{
  WM:[40,300],lakeCoefficient:[0.2,1.5],iceSublimationFraction:[0,1],iceSublimationMaxMmHour:[0,0.4]
 }},parameters:{WM:120,lakeCoefficient:1,iceSublimationFraction:0.25,iceSublimationMaxMmHour:0.08}},
 result:{parameters:{WM:156,lakeCoefficient:1.2,iceSublimationFraction:0.25,iceSublimationMaxMmHour:0.08}}}
 const rows=Object.fromEntries(calculationProcess(record).parameters.map(row=>[row.key,row]))
 assert.equal(rows.WM.phase,'flow')
 assert.equal(rows.lakeCoefficient.phase,'lake')
 assert.equal(rows.iceSublimationFraction.phase,'fixed')
 assert.equal(rows.iceSublimationMaxMmHour.phase,'fixed')
 assert.equal(rows.WM.initial,120)
 assert.equal(rows.WM.value,156)
 assert.equal(calculationProcess({request:{search:{bounds:{WM:[40,300]}}}}).parameters[0].phase,'unknown')
 record.request.twoStage.mode='lake'
 assert.equal(calculationProcess(record).parameters.find(row=>row.key==='WM').phase,'fixed')
})
