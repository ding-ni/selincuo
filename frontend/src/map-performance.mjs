// Bounded browser performance entries for measuring a layer switch without
// adding diagnostics to the business screen. Paint latency includes the
// browser's frame scheduling and is not presented as GPU execution time.
export function recordMapTiming(name,start,duration){
 const perf=globalThis.performance,key='selinco-map:'+name
 if(!perf?.measure||!Number.isFinite(start)||!Number.isFinite(duration))return
 try{
  if(perf.getEntriesByName(key).length>=64)perf.clearMeasures(key)
  perf.measure(key,{start,duration:Math.max(0,duration)})
 }catch{}
}
