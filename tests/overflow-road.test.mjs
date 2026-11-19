import assert from 'node:assert/strict'
import test from 'node:test'
import {dailyFrameIndex,scenarioExposures,exposureImpact,peakRoadInundation} from '../frontend/src/overflow-data.mjs'

const east={bounds:[0,0,4,1],width:4,height:1}
const exposures={roads:[{id:'road',coords:[[-1,.5],[5,.5]]}],settlements:[]}

test('a road crossing several cells only highlights the flooded sections',()=>{
 const result=exposureImpact({meta:{east}},{state:[0,2,4,2],depth:[0,.4,1.2,1.4]},exposures)
 assert.deepEqual(result.roads.map(r=>[r.coords,r.status]),[
  [[[1,.5],[2,.5]],'corridor'],
  [[[2,.5],[3,.5]],'overtopped'],
  [[[3,.5],[4,.5]],'closed']
 ])
 assert.equal(result.corridorKm,result.wetRoadKm)
 assert.equal(result.roadKm,result.corridorKm)
})

test('the historical road shortcut selects a day with the most exposed road',()=>{
 const data={meta:{east,datum:{lakeMinusDemM:0},bange:{surfaceDemM:0}},
             heights:[.5,1.1,1.2,2.8],domain:[1,1,2,0]}
 const frames=[{lake:1,base:1,bangeRiseM:0},{lake:1.2,base:1,bangeRiseM:0},{lake:1.2,base:1,bangeRiseM:1.4}]
 assert.equal(peakRoadInundation(data,frames,exposures),2)
 assert.equal(peakRoadInundation(data,frames.slice(0,1),exposures),-1)
})

test('imported multipart roads only highlight on the date and cells stored in the scenario archive',()=>{
 const result={rows:[
  {time:'2026-12-31T13:00:00'},{time:'2027-01-01T00:00:00'},{time:'2027-01-02T00:00:00'}
 ],frameRows:[0,1,2]}
 const archive=[{depth:[1.5,0,0,0],state:[2,0,0,0]},
  {depth:[0,1.4,0,0],state:[0,2,0,0]}, {depth:[0,0,0,0],state:[0,0,0,0]}]
 const ex=scenarioExposures([{id:'field',source:'现场测绘',features:[
  {properties:{名称:'现场道路'},geometry:{type:'MultiLineString',coordinates:[[[0,.5],[2,.5]],[[3,.5],[4,.5]]]}}
 ]}])
 const at=day=>exposureImpact({meta:{east}},archive[dailyFrameIndex(result,day)],ex)
 assert.equal(ex.roads.length,2)
 assert.deepEqual(at('2026-12-31').roads.map(r=>[r.road.name,r.coords,r.status]),[
  ['现场道路',[[1,.5],[2,.5]],'closed']
 ])
 assert.deepEqual(at('2027-01-01').roads,[])
 assert.equal(dailyFrameIndex(result,'2027-01-02'),-1)
 assert.equal(dailyFrameIndex({...result,frameRows:undefined},'2027-01-01'),2)
})
