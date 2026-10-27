import test from 'node:test'
import assert from 'node:assert/strict'
import {scaledLayer} from '../frontend/src/map-scale.mjs'
const base={id:'precipitation',colors:['a','b','c'],stops:[]}
test('replay range remains fixed across frames and zero is distinct from missing',()=>{
 const range={minimum:0,maximum:24}
 assert.deepEqual(scaledLayer(base,[null,0,1],range).stops,[0,12,24])
 assert.deepEqual(scaledLayer(base,[6,7,8],range).stops,[0,12,24])
 assert.deepEqual(scaledLayer(base,[null,0,1]).stops,[0,.5,1])
})
test('signed and physical ratio variables keep interpretable scales',()=>{
 assert.deepEqual(scaledLayer({...base,id:'temperature'},[-4,2]).stops,[-4,0,4])
 assert.deepEqual(scaledLayer({...base,id:'FR'},[.2,.3]).stops,[0,.5,1])
})
test('explicit custom bounds are honored',()=>{
 assert.deepEqual(scaledLayer(base,[0,10],null,[2,4]).stops,[2,3,4])
 assert.deepEqual(scaledLayer({...base,id:'temperature'},[-10,10],null,[-1,3]).stops,[-1,1,3])
})
