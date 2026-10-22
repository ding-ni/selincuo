export function partitionSummary(partitions, year) {
  const rows=(partitions||[]).map(p=>{
    const months=p.monthly.filter(m=>m.month.startsWith(String(year)))
    if(!months.length)return null
    const totals={}
    for(const m of months)for(const [k,v] of Object.entries(m))if(typeof v==='number'&&k!=='hours')totals[k]=(totals[k]||0)+v
    return {...p,...totals,months,hours:months.reduce((n,m)=>n+m.hours,0),completeMonths:months.filter(m=>m.complete).length}
  }).filter(Boolean)
  const total=rows.reduce((n,r)=>n+r.routedRunoffM3,0)
  return rows.map(r=>({...r,share:total>0?r.routedRunoffM3/total:null}))
}

export function comparisonDifferences(a,b) {
  if(!a||!b)return []
  const fields=[['真实输入',a.inputId,b.inputId],['空间版本',a.spatialVersion,b.spatialVersion],
    ['模型实现',a.identity?.modelImplementation,b.identity?.modelImplementation],
    ['观测快照',a.identity?.observationSHA256,b.identity?.observationSHA256],
    ['起始时刻',a.start,b.start],['结束时刻',a.end,b.end],['汇流结构',a.routingMode,b.routingMode]]
  return fields.map(([label,left,right])=>({label,left:left??'未记录',right:right??'未记录',
    status:left==null||right==null?'unknown':left===right?'same':'different'}))
}

export const inEvaluationYear=(rows,year)=>(rows||[]).filter(r=>(r.date||r.time||'').startsWith(String(year)))
