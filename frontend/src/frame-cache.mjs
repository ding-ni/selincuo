// Keep completed frames and a small prioritized request queue; stale callers never commit UI state.
export function createFrameCache(limit=36,concurrency=3){
 const entries=new Map(),queue=[];let active=0,closed=false
 function trim(){for(const [key,item] of entries){if(entries.size<=limit)break;if(item.done)entries.delete(key)}}
 function pump(){
  while(!closed&&active<concurrency&&queue.length){
   const job=queue.shift();active++;job.started=true
   Promise.resolve().then(()=>job.loader(job.controller.signal)).then(value=>{job.done=true;job.resolve(value)},error=>{if(entries.get(job.key)===job)entries.delete(job.key);job.reject(error)}).finally(()=>{active--;trim();pump()})
  }
 }
 function get(key,loader,prefetch=false){
  if(closed)return Promise.reject(new DOMException('画面已关闭','AbortError'))
  if(entries.has(key)){const entry=entries.get(key);entries.delete(key);entries.set(key,entry);return entry.promise}
  let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b}),entry={key,loader,promise,resolve,reject,controller:new AbortController(),done:false,started:false}
  entries.set(key,entry);if(prefetch)queue.push(entry);else queue.unshift(entry)
  while(queue.length>6){const old=queue.pop();entries.delete(old.key);old.reject(new DOMException('已选择其他日期','AbortError'))}
  trim();pump();return promise
 }
 function clear(){closed=true;for(const job of queue.splice(0)){job.reject(new DOMException('画面已关闭','AbortError'))}for(const job of entries.values())if(!job.done)job.controller.abort();entries.clear()}
 function cancelPending(){
  for(const job of queue.splice(0)){if(entries.get(job.key)===job)entries.delete(job.key);job.reject(new DOMException('已选择其他日期','AbortError'))}
  for(const [key,job] of entries){if(!job.done){entries.delete(key);job.controller.abort()}}
 }
 return {get,clear,cancelPending,size:()=>entries.size}
}
