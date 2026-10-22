import test from 'node:test'
import assert from 'node:assert/strict'
import {partitionSummary,comparisonDifferences,inEvaluationYear} from '../frontend/src/diagnostics.mjs'
import {runIssue} from '../frontend/src/workflow.mjs'
test('partition shares use exclusive volumes and preserve partial coverage',()=>{
 const parts=[{id:'duja',monthly:[{month:'2024-01',hours:744,complete:true,routedRunoffM3:10,precipitationMm:20},{month:'2025-01',hours:744,complete:true,routedRunoffM3:999}]},{id:'zajia_rest',monthly:[{month:'2024-01',hours:20,complete:false,routedRunoffM3:30}]}]
 const a=partitionSummary(parts,'2024')
 assert.equal(a[0].share,.25);assert.equal(a[1].share,.75)
 assert.equal(a[1].completeMonths,0);assert.equal(a[1].hours,20)
 assert.equal(partitionSummary(parts,'2026').length,0)
})
test('comparison distinguishes missing evidence, observation snapshots and years',()=>{
 const a={inputId:'real',identity:{observationSHA256:'obs-a'},daily:[]}
 const b={...a,identity:{observationSHA256:'obs-b'}}
 const diff=comparisonDifferences(a,b)
 assert.equal(diff.find(d=>d.label==='观测快照').status,'different')
 assert.equal(diff.find(d=>d.label==='模型实现').status,'unknown')
 assert.deepEqual(inEvaluationYear([{date:'2024-01-01'},{date:'2025-01-01'}],'2025'),[{date:'2025-01-01'}])
})
test('routing calibration requires frozen baseline and source-separated mode',()=>{
 const r={inputId:'real',start:'2023-01-01T00:00',end:'2026-01-01T00:00',stage:'routing',maxEvaluations:120,routingMode:'source-separated'}
 const i=[{id:'real',manifest:{start:r.start,end:r.end}}]
 assert.match(runIssue(r,i,'calibration'),/冻结/)
 assert.match(runIssue({...r,baseTaskId:'b',routingMode:'lumped'},i,'calibration'),/分水源/)
 assert.equal(runIssue({...r,baseTaskId:'b'},i,'calibration'),'')
})
