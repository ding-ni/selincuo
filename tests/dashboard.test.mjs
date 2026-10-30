import test from 'node:test'
import assert from 'node:assert/strict'
import {timelineDates,exactValue,balanceTerms,defaultReplayDay,colorFor,layers,screenResults,replayResultName,hasDashboardCapabilities,replayRows,basinRain,observationDay,timelineTicks,latestObservation,monitoringWindow,recentObservations} from '../frontend/src/dashboard.mjs'
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
test('overview excludes internal warmup by declared display coverage, without changing stored daily records',()=>{
 const result={displayCoverage:{start:'2021-04-23T00:00:00',endExclusive:'2021-04-25T00:00:00'},daily:[
  {date:'2020-01-01',lakeLevel:null},{date:'2021-04-23',lakeLevel:null,observedLevel:4540},
  {date:'2021-04-24',lakeLevel:4540.1},{date:'2021-04-25',lakeLevel:4540.2}]}
 assert.deepEqual(timelineDates('model',null,result),['2021-04-23','2021-04-24'])
 assert.deepEqual(replayRows(result).map(r=>r.date),['2021-04-23','2021-04-24'])
 assert.equal(result.daily.length,4)
 assert.equal(defaultReplayDay(timelineDates('model',null,result),result),'2021-04-24')
 assert.equal(timelineDates('model',null,{daily:result.daily}).length,4)
})
test('dashboard offers only the adopted result',()=>{
 const rows=screenResults([{id:'old-publication'},{id:'final'}],[{id:'trial',kind:'trial',status:'succeeded',userSaved:1},
  {id:'automatic',kind:'simulation',collection:'历史模拟',favorite:1,status:'succeeded'},
  {id:'user',kind:'simulation',userSaved:1,status:'succeeded'},
  {id:'continued',kind:'continuation',userSaved:1,status:'succeeded'},
  {id:'deleted',kind:'simulation',userSaved:1,status:'succeeded',trashed:1}], 'final')
 assert.deepEqual(rows.map(r=>r.id),['final'])
 assert.equal(rows[0].builtin,true)
 assert.equal(replayResultName(rows[0],'final',{id:'final',result:{daily:[{date:'2021-04-23'},{date:'2026-10-20'}]}}),'历史模拟 · 2021—2026')
 const task={status:'succeeded',result:{daily:[{date:'2025-01-01',lakeLevel:4540,total:5}],units:[{id:'unit'}],inputId:'input'}}
 const meta={units:[{id:'unit'}],spatialBase:'/spatial/version/',hours:24}
 assert.equal(hasDashboardCapabilities(task,meta,[{id:'input'}]),true)
 assert.equal(hasDashboardCapabilities(task,meta,[]),false)
 assert.equal(hasDashboardCapabilities(task,{...meta,spatialBase:null},[{id:'input'}]),false)
 assert.equal(hasDashboardCapabilities({...task,result:{...task.result,daily:[{date:'2025-01-01',lakeLevel:null,total:5}]}},meta,[{id:'input'}]),false)
})
test('basin rain weights the Duja and lower Zajia areas once and retains missing values',()=>{
 const units=[{id:'duja',partition:'duja',areaKm2:2},{id:'lower',partition:'zajia_rest',areaKm2:1},
  {id:'zero',basin:'ali',partition:'ali',areaKm2:5},{id:'extra',partition:'fringe',areaKm2:10}]
 const frame={complete:true,unitValues:[{id:'duja',value:3},{id:'lower',value:6},{id:'zero',value:0},{id:'extra',value:100}]}
 const rows=basinRain(frame,units)
 assert.equal(rows.find(r=>r.id==='zajia').value,4)
 assert.equal(rows.find(r=>r.id==='ali').value,0)
 assert.equal(rows.find(r=>r.id==='boqu').value,null)
 assert.equal(rows.some(r=>r.id==='fringe'),false)
 assert.equal(basinRain({...frame,unitValues:frame.unitValues.filter(r=>r.id!=='lower')},units)[0].value,null)
 assert.ok(basinRain({...frame,complete:false},units).every(r=>r.value===null))
})
test('selected-day observations distinguish record boundaries, missing days, zero and failure',()=>{
 const station={coverage:{start:'2025-01-01T00:00:00',end:'2025-01-03T00:00:00'},daily:[{date:'2025-01-01',value:0},{date:'2025-01-02',value:null},{date:'2025-01-03',value:2}]}
 assert.equal(observationDay(station,'2024-06-19').status,'outside')
 assert.equal(observationDay(station,'2025-01-01').value,0)
 assert.equal(observationDay(station,'2025-01-02').status,'missing')
 assert.equal(observationDay({...station,error:'offline'},'2025-01-03').status,'failed')
 const ticks=timelineTicks(['2023-04-20','2024-01-01','2025-01-01','2025-01-10'])
 assert.equal(ticks[0].position,0);assert.equal(ticks.at(-1).position,100)
 assert.ok(ticks.at(-2).position>95)
})
test('monitoring trends preserve missing dates and never show observations after the selected day',()=>{
 const station={daily:[{date:'2026-04-22',value:4544.5},{date:'2026-09-11',value:4544.7},
  {date:'2026-09-12',value:null},{date:'2026-09-14',value:4545}]}
 assert.equal(latestObservation(station,'2026-09-13').date,'2026-09-11')
 assert.equal(latestObservation(station,'2026-04-21'),null)
 assert.deepEqual(monitoringWindow(station,'2026-09-13',4),[
  {date:'2026-09-10',value:null},{date:'2026-09-11',value:4544.7},
  {date:'2026-09-12',value:null},{date:'2026-09-13',value:null}])
 assert.deepEqual(recentObservations(station,'2026-09-13').map(r=>r.date),['2026-09-11','2026-04-22'])
 assert.equal(latestObservation({...station,error:'offline'},'2026-09-13'),null)
})
