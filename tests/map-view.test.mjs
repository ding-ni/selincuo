import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import {projector,fitViewport,placeLabels} from '../frontend/src/map-view.mjs'
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
test('complete real historical input takes precedence over earlier fragments with complete end-hour days',()=>{
 const full={id:'full',manifest:{start:'2022-12-01T09:00:00',end:'2026-01-01T07:00:00',hours:27047}}
 const fragment={id:'old',manifest:{start:'2022-12-01T09:00:00',end:'2023-05-01T07:00:00',hours:3600}}
 assert.equal(preferredHistoricalInput([fragment,full]).id,'full')
 assert.deepEqual(historicalPeriod(full.manifest),{start:'2023-01-01T00:00',end:'2026-01-01T00:00'})
 assert.equal(preferredHistoricalInput([fragment,{...full,compatible:false}]).id,'old')
})
