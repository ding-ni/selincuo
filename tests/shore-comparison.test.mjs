import test from 'node:test'
import assert from 'node:assert/strict'
import {lakeStations,compareShoreLevels,stationLocation} from '../frontend/src/shore-comparison.mjs'
const sample=()=>Object.fromEntries(lakeStations.map((s,i)=>[s.id,[{time:'2025-01-01',value:100+i},{time:'2025-01-02',value:101+i},{time:'2025-01-03',value:102+i}]]))
const withReferences=data=>{for(const rows of Object.values(data))for(const row of rows){row.referenceValue=row.value;row.referenceDatum={available:true,referenceStation:'SLC'}}return data}

test('same-day diagnostics preserve original offsets and missing days',()=>{
 const data=sample();data['SLC-N'][1].value=null;data.SLC[2].value=null
 const original=JSON.stringify(data),r=compareShoreLevels(data)
 assert.equal(r.rows[0]['SLC-N'],101)
 assert.equal(r.rows[1]['SLC-N'],null)
 assert.equal(r.difference[2]['SLC-E'],null)
 assert.equal(r.summary.find(s=>s.id==='SLC-N').matchedDays,1)
 assert.equal(r.summary.find(s=>s.id==='SLC-E').median,2)
 assert.equal(JSON.stringify(data),original)
})
test('relative lake change uses one common date, never different dates by station',()=>{
 const data=sample();data['SLC-N'][0].value=null
 const r=compareShoreLevels(data)
 assert.equal(r.anchor,'2025-01-02');assert.equal(r.commonDays,2)
 assert.equal(r.relative[0]['SLC-N'],null)
 for(const s of lakeStations){assert.equal(r.relative[1][s.id],0);assert.equal(r.relative[2][s.id],1)}
 data['SLC-N'][1].value=null;data['SLC-N'][2].value=null
 const empty=compareShoreLevels(data);assert.equal(empty.anchor,null)
 assert.ok(empty.relative.every(row=>lakeStations.every(s=>row[s.id]===null)))
})
test('model missing lake windows stay missing and do not acquire a relative anchor',()=>{
 const r=compareShoreLevels(sample(),[{date:'2025-01-01',lakeLevel:null},{date:'2025-01-02',lakeLevel:101}])
 assert.equal(r.rows[0].model,null);assert.equal(r.rows[1].model,101)
 assert.equal(r.modelAtAnchor,false)
 assert.ok(r.relative.every(row=>row.model===null))
 assert.equal(r.difference[1].model,0)
})
test('station provenance accepts API JSON metadata without discarding raw coordinates',()=>{
 const location={sourceCRS:'GCJ-02',sourceLon:89.06496134,kCode:'7eqfhjzuq'}
 assert.deepEqual(stationLocation({metadata:JSON.stringify({location})}),location)
 assert.deepEqual(stationLocation({metadata:{location}}),location)
 assert.equal(stationLocation({metadata:'invalid'}),null)
})

test('absolute model evaluation keeps offsets and offers equal-date samples without filling gaps',()=>{
 const data=sample();data['SLC-N'][1].value=null;withReferences(data)
 const r=compareShoreLevels(data,[{date:'2025-01-01',lakeLevel:100},{date:'2025-01-02',lakeLevel:101},{date:'2025-01-03',lakeLevel:null}])
 assert.equal(r.evaluation[0].count,2)
 assert.equal(r.evaluation[1].count,1)
 assert.equal(r.evaluation[1].bias,-1)
 assert.equal(r.evaluation[1].mae,1)
 assert.equal(r.evaluation[1].rmse,1)
 assert.equal(r.commonModelDays,1)
 assert.ok(r.commonEvaluation.every(s=>s.count===1))
 assert.equal(r.commonEvaluation[4].bias,-4)
 assert.equal(r.residual[1]['SLC-N'],null)
 assert.equal(r.residual[2].SLC,null)
})

test('absolute evaluation requires the independently resolved reference for each station and day',()=>{
 const data=sample(),main=data.SLC,north=data['SLC-N']
 for(const row of main){row.referenceValue=row.value;row.referenceDatum={available:true,referenceStation:'SLC'}}
 north[1].referenceValue=99;north[1].referenceDatum={available:true,referenceStation:'SLC'}
 north[2].referenceValue=99;north[2].referenceDatum={available:false,referenceStation:'SLC'}
 const r=compareShoreLevels(data,[{date:'2025-01-01',lakeLevel:100},{date:'2025-01-02',lakeLevel:101},{date:'2025-01-03',lakeLevel:102}])
 assert.deepEqual(r.referenceStations,['SLC','SLC-N'])
 assert.equal(r.evaluation[0].count,3);assert.equal(r.evaluation[1].count,1)
 assert.equal(r.evaluation[1].bias,2);assert.equal(r.residual[0]['SLC-N'],null)
 assert.equal(r.residual[1]['SLC-N'],2);assert.equal(r.residual[2]['SLC-N'],null)
 assert.equal(r.evaluation[2].count,0);assert.equal(r.commonModelDays,0)
 assert.equal(r.rows[1]['SLC-N'],102)
})

test('initialization days remain visible but cannot score their own anchor',()=>{
 const r=compareShoreLevels(withReferences(sample()),[{date:'2025-01-01',lakeLevel:100},{date:'2025-01-02',lakeLevel:101}],['2025-01-01'])
 assert.equal(r.rows[0].model,100)
 assert.equal(r.evaluation[0].count,1)
 assert.equal(r.evaluation[0].start,'2025-01-02')
 assert.equal(r.commonModelDays,1)
 assert.ok(r.commonEvaluation.every(s=>s.count===1))
})
