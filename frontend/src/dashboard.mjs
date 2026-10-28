export const rivers=[{id:'zajia',name:'扎加藏布',short:'扎加'},{id:'zagen',name:'扎根藏布',short:'扎根'},{id:'boqu',name:'波曲藏布',short:'波曲'},{id:'ali',name:'阿里藏布',short:'阿里'},{id:'fringe',name:'湖周直接来水',short:'湖周'}]
export const layers=[{id:'precipitation',label:'模型采用日降水',unit:'mm/d',colors:['#edf6ee','#bfdcc6','#7fbfb2','#439fa7','#28758f','#22516f'],stops:[0,.1,1,3,8,20]},
 {id:'temperature',label:'日均气温',unit:'℃',colors:['#486caa','#77a7c1','#afcfcb','#e6d99e','#dcb06c','#bd7554'],stops:[-30,-15,-5,0,5,15]},
 {id:'evaporation',label:'日蒸发能力',unit:'mm/d',colors:['#eef1d1','#dfdf9e','#ccc37a','#c6a15b','#b67a40','#96562f'],stops:[0,1,2,4,6,8]},
 {id:'snow',label:'日末雪水当量',unit:'mm',colors:['#e8edf0','#c4dcea','#91bfd9','#6593be','#536c9c','#534e80'],stops:[0,1,10,30,100,300]},
 {id:'terrain',label:'地形高程',unit:'m',colors:['#78998a','#a5b299','#cac7a1','#c5b093','#b1a596','#ebe9e2'],stops:[4400,4600,4800,5000,5500,6000]},
 {id:'basin',label:'流域分区',unit:'',colors:[],stops:[]}]
export function colorFor(value,layer){if(value==null||!Number.isFinite(value))return 'transparent';let i=0;for(let j=0;j<layer.stops.length;j++)if(value>=layer.stops[j])i=j;return layer.colors[i]||'transparent'}
export function exactValue(station,day){return station?.daily?.find(r=>r.date===day)?.value??null}
export function observationDay(station,day){
 if(station?.error)return {status:'failed',label:'读取失败',value:null}
 const row=station?.daily?.find(r=>r.date===day),value=row?.value
 if(value!=null&&Number.isFinite(value))return {status:'valid',label:'有效记录',value}
 const first=(station?.coverage?.start||station?.daily?.[0]?.date||'').slice(0,10),last=(station?.coverage?.end||station?.daily?.at(-1)?.date||'').slice(0,10)
 if(!first||day<first||day>last)return {status:'outside',label:'该时段无记录',value:null}
 return {status:'missing',label:'当日无有效日值',value:null}
}
export function timelineTicks(dates=[]){
 if(!dates.length)return []
 const first=Date.parse(dates[0]+'T00:00:00Z'),last=Date.parse(dates.at(-1)+'T00:00:00Z'),days=[dates[0],...dates.filter(d=>d.endsWith('-01-01')),dates.at(-1)]
 return [...new Set(days)].map(date=>({date,position:last===first?0:(Date.parse(date+'T00:00:00Z')-first)/(last-first)*100}))
}
export function basinRain(frame,units=[]){
 const values=new Map((frame?.unitValues||[]).map(u=>[u.id,u.value]))
 return rivers.filter(r=>r.id!=='fringe').map(r=>{
  let total=0,area=0,missing=!frame?.complete
  for(const u of units){const basin=['duja','zajia_rest'].includes(u.partition)?'zajia':u.basin||u.partition;if(basin!==r.id)continue;const value=values.get(u.id);if(value==null||!Number.isFinite(value))missing=true;else{total+=value*u.areaKm2;area+=u.areaKm2}}
  return {...r,value:missing||!area?null:total/area}
 })
}
export function replayRows(result){
 const rows=result?.daily||[],coverage=result?.displayCoverage
 if(!coverage)return rows
 const start=(coverage.start||'').slice(0,10),end=(coverage.endExclusive||'').slice(0,10)
 return rows.filter(row=>{const day=(row.date||'').slice(0,10);return day&&(!start||day>=start)&&(!end||day<end)})
}
export function timelineDates(mode,monitor,result){
 if(mode==='model')return replayRows(result).map(r=>r.date.slice(0,10))
 const all=(monitor?.stations||[]).flatMap(s=>s.daily||[]).map(r=>r.date).sort()
 if(!all.length)return []
 const start=Date.parse(all[0]+'T00:00:00Z'),end=Date.parse(all.at(-1)+'T00:00:00Z'),dates=[]
 for(let t=start;t<=end;t+=86400000)dates.push(new Date(t).toISOString().slice(0,10))
 return dates
}
export function balanceTerms(row){return [{key:'lakeInflowM3',label:'入湖来水',sign:1},{key:'lakePrecipitationM3',label:'湖面降水',sign:1},{key:'lakeEvaporationM3',label:'湖面蒸发',sign:-1},{key:'lakeDeltaVolumeM3',label:'蓄水变化',sign:1}].map(t=>({...t,value:row?.[t.key]==null?null:row[t.key]/1e6}))}
export function defaultReplayDay(dates,result){const latest=replayRows(result).filter(r=>r.lakeLevel!=null).at(-1);return latest?.date.slice(0,10)||dates.at(-1)||''}
// Only the adopted result and results the user explicitly saved belong on the overview.
// Callers verify the latter's full display capabilities before offering them.
export function screenResults(published=[],tasks=[],homeId=''){
 const home=published.find(r=>r.id===homeId)||tasks.find(r=>r.id===homeId)
 const adopted=home&&!home.trashed?[{...home,builtin:published.some(r=>r.id===homeId)}]:homeId?[{id:homeId}]:[]
 const saved=tasks.filter(t=>t.id!==homeId&&t.status==='succeeded'&&!t.trashed&&['simulation','continuation'].includes(t.kind)&&!!t.userSaved)
 return [...adopted,...saved]
}
export function replayResultName(t,homeId,loaded){
 if(t.id!==homeId)return t.name||t.displayName||'已保存模拟'
 const result=loaded?.id===t.id?loaded.result:null,rows=result?replayRows(result):null
 const start=(rows?.[0]?.date||result?.displayCoverage?.start||t.request?.start||'').slice(0,4)
 const end=(rows?.at(-1)?.date||result?.displayCoverage?.endExclusive||t.request?.end||'').slice(0,4)
 return start&&end?'历史模拟 · '+start+(start===end?'':'—'+end):'历史模拟'
}
export function hasDashboardCapabilities(task,metadata,inputs=[]){
 const result=task?.result
 return !!(task?.status==='succeeded'&&replayRows(result).some(day=>day.date&&Number.isFinite(day.lakeLevel)&&Number.isFinite(day.total))&&result?.units?.length&&result?.inputId&&
  inputs.some(input=>input.id===result.inputId)&&metadata?.spatialBase&&metadata?.units?.length&&metadata?.hours>0)
}
