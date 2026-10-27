export const lakeStations=[{id:'SLC',name:'主站'},{id:'SLC-N',name:'北岸'},{id:'SLC-E',name:'东岸'},{id:'SLC-S',name:'南岸'},{id:'SLC-W',name:'西岸'}]
const valid=v=>typeof v==='number'&&Number.isFinite(v)
const quantile=(sorted,p)=>{if(!sorted.length)return null;const x=(sorted.length-1)*p,i=Math.floor(x);return sorted[i]+(sorted[Math.ceil(x)]-sorted[i])*(x-i)}

export function compareShoreLevels(data,modelRows=[],excludedDays=[]){
 const maps=Object.fromEntries(lakeStations.map(s=>[s.id,new Map((data[s.id]||[]).map(r=>[(r.time||r.date).slice(0,10),r.value]))]))
 const model=new Map(modelRows.map(r=>[r.date.slice(0,10),r.lakeLevel]))
 const dates=[...new Set(lakeStations.flatMap(s=>[...maps[s.id].keys()]))].sort()
 const rows=dates.map(date=>({date,...Object.fromEntries(lakeStations.map(s=>[s.id,valid(maps[s.id].get(date))?maps[s.id].get(date):null])),model:valid(model.get(date))?model.get(date):null}))
 const anchor=rows.find(r=>lakeStations.every(s=>valid(r[s.id])))||null
 const relative=rows.map(r=>({date:r.date,...Object.fromEntries([...lakeStations.map(s=>s.id),'model'].map(k=>[k,anchor&&valid(anchor[k])&&valid(r[k])?r[k]-anchor[k]:null]))}))
 const difference=rows.map(r=>({date:r.date,...Object.fromEntries([...lakeStations.map(s=>s.id),'model'].map(k=>[k,valid(r.SLC)&&valid(r[k])?r[k]-r.SLC:null]))}))
 const residual=rows.map(r=>({date:r.date,...Object.fromEntries(lakeStations.map(s=>[s.id,valid(r.model)&&valid(r[s.id])?r.model-r[s.id]:null]))}))
 const excluded=new Set(excludedDays),scoredRows=rows.filter(r=>!excluded.has(r.date))
 const commonModelRows=scoredRows.filter(r=>valid(r.model)&&lakeStations.every(s=>valid(r[s.id])))
 const metrics=selected=>lakeStations.map(s=>{
  const pair=selected.filter(r=>valid(r.model)&&valid(r[s.id])),error=pair.map(r=>r.model-r[s.id]),n=error.length
  return {...s,count:n,start:pair[0]?.date||null,end:pair.at(-1)?.date||null,bias:n?error.reduce((a,b)=>a+b,0)/n:null,mae:n?error.reduce((a,b)=>a+Math.abs(b),0)/n:null,rmse:n?Math.sqrt(error.reduce((a,b)=>a+b*b,0)/n):null}
 })
 const summary=lakeStations.map(s=>{
  const values=rows.filter(r=>valid(r[s.id])),pairs=rows.filter(r=>valid(r[s.id])&&valid(r.SLC)),delta=pairs.map(r=>r[s.id]-r.SLC).sort((a,b)=>a-b)
  return {...s,days:values.length,start:values[0]?.date||null,end:values.at(-1)?.date||null,matchedDays:pairs.length,median:quantile(delta,.5),p05:quantile(delta,.05),p95:quantile(delta,.95)}
 })
 return {rows,relative,difference,residual,summary,evaluation:metrics(scoredRows),commonEvaluation:metrics(commonModelRows),commonModelDays:commonModelRows.length,anchor:anchor?.date||null,commonDays:rows.filter(r=>lakeStations.every(s=>valid(r[s.id]))).length,modelAtAnchor:!!anchor&&valid(anchor.model)}
}

function stationMetadata(station){
 try{return typeof station?.metadata==='string'?JSON.parse(station.metadata):station?.metadata}catch{return null}
}
export const stationLocation=station=>stationMetadata(station)?.location||null
export const stationDatum=station=>stationMetadata(station)?.verticalDatum||null
export const commonLakeDatum=stations=>lakeStations.every(s=>{const meta=stationMetadata(stations.find(r=>r.id===s.id));return meta?.datumVerified===true&&meta.verticalDatum?.referenceStation==='SLC'&&meta.verticalDatum?.zeroUnified===true})
