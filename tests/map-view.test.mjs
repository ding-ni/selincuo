import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import {projector,fitViewport,placeLabels,placeGeographicLabels,leaderEnd,detailMapUrl} from '../frontend/src/map-view.mjs'
import {preferredHistoricalInput,historicalPeriod} from '../frontend/src/workflow.mjs'
const context=JSON.parse(fs.readFileSync(new URL('../data/derived/spatial/display-context.json',import.meta.url)))
test('whole drainage domain fits desktop, portrait and report canvases without distortion or clipping',()=>{
 for(const size of [{width:920,height:385},{width:1920,height:1080},{width:350,height:430}]){
  const fit=fitViewport(context.domainBounds,size),p=projector(context.domainBounds)
  const [w,h]=p([context.domainBounds[2],context.domainBounds[1]])
  assert.ok(fit.x>=31.99&&fit.y>=31.99)
  assert.ok(fit.x+w*fit.scale<=size.width-31.99)
  assert.ok(fit.y+h*fit.scale<=size.height-31.99)
  assert.ok(Math.min(fit.x,fit.y)<32.01)
 }
 const labels=new Map(context.labels.map(l=>[l.id,l]))
 assert.ok(labels.get('zajia').anchor[0]>90)
 assert.ok(labels.get('zagen').anchor[0]<89)
 assert.ok(labels.get('boqu').anchor[0]>89.2)
 assert.ok(labels.get('ali').anchor[0]<89.2)
})
test('nearby basin label boxes avoid overlap and keep true geographic anchors',()=>{
 const rows=placeLabels([{id:'a',x:180,y:150},{id:'b',x:185,y:155}],{width:500,height:300})
 const [a,b]=rows.map(r=>r.box)
 assert.ok(a.y+a.h<=b.y||b.y+b.h<=a.y||a.x+a.w<=b.x||b.x+b.w<=a.x)
 assert.equal(rows[1].x,185)
})
test('legend and map controls are avoided as reserved areas without changing label anchors',()=>{
 const legend={x:345,y:175,w:145,h:110},controls={x:330,y:5,w:160,h:32}
 const rows=placeLabels([{id:'boqu',x:365,y:210,width:130,height:24},{id:'main',x:365,y:20,width:65,height:22,priority:100}],{width:500,height:300},[legend,controls])
 for(const {box} of rows)for(const b of [legend,controls])assert.ok(box.x+box.w<=b.x||b.x+b.w<=box.x||box.y+box.h<=b.y||b.y+b.h<=box.y)
 assert.equal(rows.find(x=>x.id==='boqu').x,365)
})
test('shared label priority and preferred offsets are stable across input order',()=>{
 const input=[{id:'minor',x:180,y:150,priority:1,preferredOffset:[0,0]},
  {id:'main',x:185,y:150,priority:100,preferredOffset:[35,-18]},
  {id:'shore',x:185,y:150,priority:95,preferredOffset:[-35,18]}]
 const size={width:500,height:300},first=placeLabels(input,size),again=placeLabels([...input].reverse(),size)
 assert.deepEqual(first.map(x=>x.id),['main','shore','minor'])
 assert.deepEqual(first.map(x=>x.box),again.map(x=>x.box))
 assert.equal(first[0].cx,220);assert.equal(first[0].cy,132)
 assert.equal(first[0].x,185);assert.equal(first[0].y,150)
})
test('geographic names retain their anchor while nearby names wrap and station leaders stop at the label edge',()=>{
 const names=[{id:'boqu',name:'波曲藏布',kind:'basin',x:290,y:160},
  {id:'ali',name:'阿里藏布',kind:'basin',x:244,y:156}]
 const result=placeGeographicLabels(names)
 assert.deepEqual(result.map(r=>[r.cx,r.cy]),[[290,160],[244,156]])
 assert.deepEqual(result.map(r=>r.lines.length),[2,2])
 const [a,b]=result.map(r=>r.box)
 assert.ok(a.x+a.w<=b.x||b.x+b.w<=a.x||a.y+a.h<=b.y||b.y+b.h<=a.y)
 assert.deepEqual(leaderEnd([0,20],{x:20,y:10,w:40,h:20}),[20,20])
 assert.deepEqual(leaderEnd([40,0],{x:20,y:10,w:40,h:20}),[40,10])
})
test('fine boundary detail is selected only from the matching recorded spatial parent',()=>{
 const manifest={unitStrategy:'partition-elevation-surface-area-weighted-v1',parentSpatialVersion:'GRID-SPATIAL-abc',sourceAssetsSHA256:{'display.geojson':'a'.repeat(64)}}
 assert.equal(detailMapUrl('/spatial/versions/AGG-SPATIAL-xyz/',manifest),'/spatial/versions/GRID-SPATIAL-abc/map.geojson')
 assert.equal(detailMapUrl('/spatial/versions/AGG-SPATIAL-xyz/',{...manifest,sourceAssetsSHA256:{}}),null)
 assert.equal(detailMapUrl('/spatial/versions/AGG-SPATIAL-xyz/',{...manifest,parentSpatialVersion:'../other'}),null)
 assert.equal(detailMapUrl('/spatial/versions/GRID-SPATIAL-abc/',{}),'/spatial/versions/GRID-SPATIAL-abc/map.geojson')
})
test('complete real historical input takes precedence over earlier fragments with complete end-hour days',()=>{
 const full={id:'full',manifest:{start:'2022-12-01T09:00:00',end:'2026-01-01T07:00:00',hours:27047}}
 const fragment={id:'old',manifest:{start:'2022-12-01T09:00:00',end:'2023-05-01T07:00:00',hours:3600}}
 assert.equal(preferredHistoricalInput([fragment,full]).id,'full')
 assert.deepEqual(historicalPeriod(full.manifest),{start:'2023-01-01T00:00',end:'2026-01-01T00:00'})
 assert.equal(preferredHistoricalInput([fragment,{...full,compatible:false}]).id,'old')
})
