import test from 'node:test'
import assert from 'node:assert/strict'
import {createTraceLoader} from '../frontend/src/calibration-trace-loader.mjs'

test('overlapping progress loads cannot duplicate rows or append a stale task response',async()=>{
 let id='A',rows=[],events=[],requests=[]
 const loader=createTraceLoader({getTaskId:()=>id,getOffset:()=>rows.length,
  fetchPage:(task,after)=>new Promise((resolve,reject)=>requests.push({task,after,resolve,reject})),
  onStart:()=>events.push('start'),onSuccess:page=>rows.push(...page.trace),
  onFailure:error=>events.push(error.message),onFinish:()=>events.push('finish')})
 const first=loader.load(),overlap=loader.load()
 assert.equal(requests.length,1);assert.equal(first,overlap)
 requests[0].resolve({trace:[1,2]})
 await new Promise(resolve=>setTimeout(resolve,0))
 assert.deepEqual(rows,[1,2]);assert.equal(requests[1].after,2)
 requests[1].resolve({trace:[3]});await first
 assert.deepEqual(rows,[1,2,3])
 const old=loader.load();id='B';rows=[];loader.reset()
 const current=loader.load();requests[3].resolve({trace:['B']});await current
 requests[2].resolve({trace:['stale']});await old
 assert.deepEqual(rows,['B'])
 const failure=loader.load();requests[4].reject(new Error('offline'));await failure
 assert.equal(events.at(-2),'offline');assert.equal(events.at(-1),'finish')
})
