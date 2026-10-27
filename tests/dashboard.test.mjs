import test from 'node:test'
import assert from 'node:assert/strict'
import {timelineDates,exactValue,balanceTerms,defaultReplayDay,colorFor,layers,screenResults} from '../frontend/src/dashboard.mjs'
test('monitoring replay includes missing days but never borrows the latest observation',()=>{
 const monitor={stations:[{id:'SLC',daily:[{date:'2024-01-01',value:4540},{date:'2024-01-03',value:4541}]}]}
 const dates=timelineDates('observation',monitor,null)
 assert.deepEqual(dates,['2024-01-01','2024-01-02','2024-01-03'])
 assert.equal(exactValue(monitor.stations[0],'2024-01-02'),null)
 assert.equal(exactValue(monitor.stations[0],'2024-01-01'),4540)
})
test('replay reaches latest simulated lake date despite missing observation and preserves missing budget',()=>{
 const result={daily:[{date:'2024-01-01',lakeLevel:4540,observedLevel:4540},{date:'2024-01-02',lakeLevel:4540.1,observedLevel:null}]}
 assert.equal(defaultReplayDay(['2024-01-01','2024-01-02'],result),'2024-01-02')
 assert.ok(balanceTerms(undefined).every(t=>t.value===null))
 const budget=balanceTerms({lakeInflowM3:2000000,lakePrecipitationM3:1000000,lakeEvaporationM3:500000,lakeDeltaVolumeM3:2500000})
 assert.equal(budget[2].sign,-1);assert.equal(budget[0].value,2)
 assert.equal(colorFor(null,layers[0]),'transparent')
 assert.notEqual(colorFor(0,layers[0]),'transparent')
})
test('dashboard defaults to the final publication and excludes research tasks',()=>{
 const rows=screenResults([{id:'final'}],[{id:'trial',kind:'trial',status:'succeeded'},
  {id:'user',kind:'simulation',collection:'历史模拟',status:'succeeded'},
  {id:'deleted',kind:'simulation',collection:'历史模拟',status:'succeeded',trashed:1}])
 assert.deepEqual(rows.map(r=>r.id),['final','user'])
 assert.equal(rows[0].builtin,true)
})
