import test from 'node:test'
import assert from 'node:assert/strict'
import {createFrameCache} from '../frontend/src/frame-cache.mjs'
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve}}
test('rapid date requests keep bounded concurrency and prioritize the latest pending date',async()=>{
 const cache=createFrameCache(4,1),first=deferred(),order=[]
 const a=cache.get('a',()=>{order.push('a');return first.promise})
 const b=cache.get('b',async()=>{order.push('b');return 2})
 const c=cache.get('c',async()=>{order.push('c');return 3})
 await Promise.resolve();assert.deepEqual(order,['a']);first.resolve(1)
 assert.deepEqual(await Promise.all([a,b,c]),[1,2,3]);assert.deepEqual(order,['a','c','b']);cache.clear()
})
test('prefetched complete frames reuse one request, evict old frames, and failed requests can retry',async()=>{
 const cache=createFrameCache(2,1);let reads=0
 const loader=async()=>++reads
 const a=cache.get('a',loader,true);assert.equal(await cache.get('a',loader),await a);assert.equal(reads,1)
 await cache.get('b',loader);await cache.get('c',loader);await new Promise(r=>setImmediate(r));assert.ok(cache.size()<=2)
 await assert.rejects(cache.get('bad',async()=>{throw Error('request failed')}),/request failed/)
 assert.equal(await cache.get('bad',async()=>8),8);cache.clear()
})
test('closing the dashboard cancels active frame fetches',async()=>{
 const cache=createFrameCache(2,1),started=deferred()
 const request=cache.get('a',signal=>new Promise((resolve,reject)=>{signal.addEventListener('abort',()=>reject(new DOMException('closed','AbortError')));started.resolve()}))
 await started.promise;cache.clear();await assert.rejects(request,{name:'AbortError'})
})

test('new selection aborts obsolete loads without deleting the replacement',async()=>{
 const cache=createFrameCache(8,1)
 let oldStarted
 const started=new Promise(resolve=>oldStarted=resolve)
 const first=cache.get('same',signal=>new Promise((resolve,reject)=>{
  oldStarted()
  signal.addEventListener('abort',()=>reject(new DOMException('stale','AbortError')))
 }))
 await started
 cache.cancelPending()
 const second=cache.get('same',()=>Promise.resolve({day:'new'}))
 await assert.rejects(first,{name:'AbortError'})
 assert.deepEqual(await second,{day:'new'})
 assert.deepEqual(await cache.get('same',()=>{throw Error('not cached')}),{day:'new'})
 cache.clear()
})
